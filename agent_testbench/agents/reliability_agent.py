from __future__ import annotations

from typing import Any

from .common import AgentResult, clamp, value_at
from .feature_store import robust_profile, robust_z, trend


def _risk_class(score: float) -> str:
    if score >= 0.8:
        return "critical"
    if score >= 0.55:
        return "elevated"
    if score >= 0.35:
        return "watch"
    return "acceptable"


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    unit = process_state.get("unit_242000_telemetry", {})
    dq = process_state.get("agent_results", {}).get("data_quality", {})

    timestamp = process_state.get("timestamp", "")
    feature_values = {
        "T5": value_at(unit, "T5"),
        "P13": value_at(unit, "P13"),
        "F26": value_at(unit, "F26"),
        "F9": value_at(unit, "F9"),
        "F15": value_at(unit, "F15"),
    }

    weights = {
        "T5": 0.35,
        "P13": 0.2,
        "F26": 0.18,
        "F9": 0.12,
        "F15": 0.15,
    }
    score = 0.0
    factors = []
    forbidden_changes = []
    components = {}

    for tag, value in feature_values.items():
        profile = robust_profile("unit_242000_telemetry", tag)
        z_score = robust_z("unit_242000_telemetry", tag, value)
        tag_trend = trend("unit_242000_telemetry", "date", tag, timestamp)
        p05 = profile["p05"]
        p95 = profile["p95"]
        median = profile["median"]

        if value is None or p05 is None or p95 is None or median is None:
            component_score = 0.55
            factors.append(f"{tag}: значение недоступно, риск повышен из-за неопределённости")
        else:
            span = max(p95 - p05, 1e-9)
            if tag in {"T5", "P13", "F15"}:
                percentile_position = (value - p05) / span
            else:
                percentile_position = abs(value - median) / (span / 2)
            z_component = 0.0 if z_score is None else min(abs(z_score) / 4.0, 1.0)
            trend_component = 0.0
            if tag_trend["delta"] is not None:
                trend_component = min(abs(tag_trend["delta"]) / span, 1.0)
            component_score = clamp(
                0.65 * percentile_position + 0.25 * z_component + 0.10 * trend_component
            )

        weighted = component_score * weights[tag]
        score += weighted
        components[tag] = {
            "value": value,
            "profile": profile,
            "robust_z": z_score,
            "trend": tag_trend,
            "component_score": round(component_score, 3),
            "weighted_score": round(weighted, 3),
        }

        if component_score >= 0.7:
            factors.append(f"{tag}: высокий вклад в риск ({component_score:.2f})")

    t5 = feature_values["T5"]
    if t5 is not None:
        t5_profile = robust_profile("unit_242000_telemetry", "T5")
        if t5_profile["p75"] is not None and t5 >= t5_profile["p75"]:
            forbidden_changes.append(
                {
                    "parameter": "T5",
                    "direction": "increase",
                    "reason": "T5 уже выше исторического p75, повышение усилит тяжесть режима",
                }
            )

    p13 = feature_values["P13"]
    if p13 is not None:
        p13_profile = robust_profile("unit_242000_telemetry", "P13")
        if p13_profile["p75"] is not None and p13 >= p13_profile["p75"]:
            forbidden_changes.append(
                {
                    "parameter": "P13",
                    "direction": "increase",
                    "reason": "P13 уже выше исторического p75",
                }
            )

    confidence = float(dq.get("confidence", 0.65))
    if dq.get("data_status") == "limited":
        confidence -= 0.08

    score = clamp(score)
    confidence = clamp(confidence)

    result = AgentResult(
        agent="reliability_agent",
        title="Reliability Agent",
        input_summary={
            "data_status": dq.get("data_status"),
            "features": feature_values,
            "history_table": "unit_242000_telemetry",
        },
        analysis_steps=[
            "Для T5, P13, F26, F9 и F15 рассчитаны исторические профили p05/p50/p95.",
            "Каждый текущий сигнал сравнен с историей через percentile score и robust z-score.",
            "Добавлен trend-компонент по последнему окну телеметрии.",
            "Вклады признаков взвешены и собраны в интегральный risk_score.",
            "Запреты на изменения сформированы динамически относительно исторического p75.",
        ],
        output={
            "risk_score": round(score, 3),
            "risk_class": _risk_class(score),
            "risk_components": components,
            "limiting_factors": factors or ["ограничивающих факторов в тестовом срезе не найдено"],
            "forbidden_changes": forbidden_changes,
            "confidence": round(confidence, 3),
        },
    )
    return result.to_dict()
