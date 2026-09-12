from __future__ import annotations

from typing import Any

from .common import AgentResult, CONTROLLED_PARAMETERS, clamp, value_at


def _scenario(
    scenario_id: str,
    parameter: str,
    current: float,
    proposed: float,
    base_sulfur: float,
    base_risk: float,
    confidence: float,
    reason: str,
    sensitivity: float = 0.0,
) -> dict[str, Any]:
    delta = proposed - current
    expected_sulfur = base_sulfur + delta * sensitivity
    expected_risk = base_risk
    production_proxy = 0.0
    energy_proxy = 0.0

    if parameter == "T5":
        expected_risk += delta * 0.025
        energy_proxy += delta * 0.01
    if parameter == "F26":
        expected_risk += abs(delta) * 0.004
        production_proxy += delta / max(current, 1.0)
    if parameter == "F9":
        expected_risk += abs(delta) * 0.003
        energy_proxy += abs(delta) * 0.004

    return {
        "scenario_id": scenario_id,
        "changes": [
            {
                "parameter": parameter,
                "current": round(current, 3),
                "proposed": round(proposed, 3),
                "unit": CONTROLLED_PARAMETERS[parameter]["unit"],
            }
        ],
        "expected_effect": {
            "sulfur_mg_kg": round(max(0.0, expected_sulfur), 3),
            "reliability_risk_score": round(clamp(expected_risk), 3),
            "production_proxy": round(production_proxy, 3),
            "energy_proxy": round(energy_proxy, 3),
        },
        "confidence": round(clamp(confidence), 3),
        "reason": reason,
        "model_used": {
            "quality_sensitivity_mg_kg_per_unit": round(sensitivity, 8),
            "base_sulfur_mg_kg": round(base_sulfur, 3),
            "base_reliability_risk_score": round(base_risk, 3),
        },
    }


def _candidate_deltas(parameter: str, current: float) -> list[float]:
    if parameter == "T5":
        return [-4.0, -2.0, 2.0, 4.0]
    if parameter in {"F26", "F9", "F30"}:
        return [-0.03 * current, -0.015 * current, 0.015 * current, 0.03 * current]
    return []


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    unit = process_state.get("unit_242000_telemetry", {})
    results = process_state.get("agent_results", {})
    dq = results.get("data_quality", {})
    quality = results.get("quality_state", {})
    reliability = results.get("reliability_state", {})

    forecast = quality.get("forecast_quality", {})
    base_sulfur = float(forecast.get("sulfur_mg_kg") or 9.0)
    base_risk = float(reliability.get("risk_score") or 0.5)
    base_confidence = min(
        float(dq.get("confidence", 0.65)),
        float(quality.get("confidence", 0.65)),
        float(reliability.get("confidence", 0.65)),
    )

    model = quality.get("model", {})
    sensitivities = model.get("coefficients", {})
    risk_components = reliability.get("risk_components", {})
    forbidden = reliability.get("forbidden_changes", [])
    forbidden_pairs = {(item.get("parameter"), item.get("direction")) for item in forbidden}

    scenarios = []
    scenario_id = 1
    available_values = {
        "T5": value_at(unit, "T5"),
        "F26": value_at(unit, "F26"),
        "F9": value_at(unit, "F9"),
        "F30": value_at(process_state.get("avt_telemetry", {}), "F30"),
    }

    for parameter, current in available_values.items():
        if current is None:
            continue
        limits = CONTROLLED_PARAMETERS[parameter]
        sensitivity = float(sensitivities.get(parameter, 0.0))
        risk_component = risk_components.get(parameter, {}).get("component_score", 0.4)
        for delta in _candidate_deltas(parameter, current):
            proposed = current + delta
            if proposed < limits["min"] or proposed > limits["max"]:
                continue

            direction = "increase" if delta > 0 else "decrease" if delta < 0 else "hold"
            if (parameter, direction) in forbidden_pairs:
                confidence = base_confidence - 0.25
                reason = f"{parameter} {direction} проверяется, но Reliability уже пометил направление как риск"
            else:
                confidence = base_confidence - abs(delta / max(abs(current), 1.0)) * 0.5
                if sensitivity == 0:
                    reason = f"{parameter}: сценарий по режимному параметру без обученной sulfur-чувствительности"
                elif delta * sensitivity < 0:
                    reason = f"{parameter}: модель ожидает снижение sulfur при таком изменении"
                else:
                    reason = f"{parameter}: модель ожидает рост sulfur, сценарий нужен для tradeoff-проверки"

            if risk_component and risk_component >= 0.7 and direction == "increase":
                confidence -= 0.08
                reason += "; признак уже даёт высокий вклад в риск"

            scenarios.append(
                _scenario(
                    f"S{scenario_id}",
                    parameter,
                    current,
                    proposed,
                    base_sulfur,
                    base_risk,
                    confidence,
                    reason,
                    sensitivity=sensitivity,
                )
            )
            scenario_id += 1

    if not scenarios:
        scenarios.append(
            {
                "scenario_id": "S0",
                "changes": [],
                "expected_effect": {
                    "sulfur_mg_kg": round(base_sulfur, 3),
                    "reliability_risk_score": round(base_risk, 3),
                    "production_proxy": 0.0,
                    "energy_proxy": 0.0,
                },
                "confidence": round(base_confidence, 3),
                "reason": "нет доступных управляемых параметров, оставить режим без изменений",
            }
        )

    result = AgentResult(
        agent="scenario_agent",
        title="Scenario Agent",
        input_summary={
            "data_status": dq.get("data_status"),
            "forecast_sulfur_mg_kg": base_sulfur,
            "reliability_risk_score": base_risk,
            "controlled_parameters": sorted(CONTROLLED_PARAMETERS),
            "model_features": model.get("features"),
            "generated_candidates": len(scenarios),
        },
        analysis_steps=[
            "Получены ограничения DQ, Quality и Reliability из Blackboard.",
            "Из Quality Agent взяты коэффициенты регрессионной модели sulfur.",
            "Из Reliability Agent взяты риск-компоненты и запреты на направления изменений.",
            "Для каждого управляемого параметра перебраны несколько малых delta внутри модельных диапазонов.",
            "Для каждого сценария оценены ожидаемая сера, риск, production_proxy и energy_proxy на основе чувствительности.",
            "Финальный выбор не выполнялся: сценарии переданы в Safety Gate.",
        ],
        output={"candidate_scenarios": scenarios},
    )
    return result.to_dict()
