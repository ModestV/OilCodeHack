from __future__ import annotations

from typing import Any

from .common import SULFUR_LIMIT_MG_KG, AgentResult, CONTROLLED_PARAMETERS


def _violates_forbidden(change: dict[str, Any], forbidden: list[dict[str, Any]]) -> str | None:
    parameter = change.get("parameter")
    current = float(change.get("current", 0))
    proposed = float(change.get("proposed", 0))
    direction = "increase" if proposed > current else "decrease" if proposed < current else "hold"

    for item in forbidden:
        if item.get("parameter") == parameter and item.get("direction") == direction:
            return item.get("reason", f"{parameter} {direction} forbidden")
    return None


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    results = process_state.get("agent_results", {})
    dq = results.get("data_quality", {})
    reliability = results.get("reliability_state", {})
    scenario_state = results.get("candidate_scenarios", {})
    scenarios = scenario_state.get("candidate_scenarios", [])

    accepted = []
    rejected = []
    forbidden_changes = reliability.get("forbidden_changes", [])

    for scenario in scenarios:
        reasons = []
        for change in scenario.get("changes", []):
            parameter = change.get("parameter")
            if parameter not in CONTROLLED_PARAMETERS:
                reasons.append(f"parameter {parameter} is not controlled")
                continue

            proposed = float(change.get("proposed", 0))
            limits = CONTROLLED_PARAMETERS[parameter]
            if proposed < limits["min"] or proposed > limits["max"]:
                reasons.append(f"{parameter} proposed value outside model range")

            forbidden_reason = _violates_forbidden(change, forbidden_changes)
            if forbidden_reason:
                reasons.append(f"{parameter} change forbidden by Reliability Agent: {forbidden_reason}")

        effect = scenario.get("expected_effect", {})
        sulfur = effect.get("sulfur_mg_kg")
        if sulfur is None:
            reasons.append("forecast_sulfur_mg_kg is unavailable")
        elif float(sulfur) > SULFUR_LIMIT_MG_KG:
            reasons.append("forecast_sulfur_mg_kg > 10")

        risk = effect.get("reliability_risk_score")
        if risk is not None and float(risk) >= 0.8:
            reasons.append("reliability_risk_score >= 0.8")

        if not dq.get("can_recommend", False):
            reasons.append("DQ Agent returned can_recommend = false")

        if reasons:
            rejected.append(
                {
                    "scenario_id": scenario.get("scenario_id"),
                    "reasons": reasons,
                    "scenario": scenario,
                }
            )
        else:
            accepted.append(scenario)

    result = AgentResult(
        agent="safety_gate",
        title="Safety Gate",
        input_summary={
            "candidate_count": len(scenarios),
            "can_recommend": dq.get("can_recommend"),
            "forbidden_changes": forbidden_changes,
            "sulfur_limit_mg_kg": SULFUR_LIMIT_MG_KG,
        },
        analysis_steps=[
            "Проверен whitelist управляемых параметров.",
            "Проверены модельные диапазоны proposed-значений.",
            "Проверен предел sulfur <= 10 mg/kg.",
            "Проверены запреты Reliability Agent и флаг can_recommend от DQ.",
        ],
        output={
            "accepted_scenarios": accepted,
            "rejected_scenarios": rejected,
            "can_issue_recommendation": bool(accepted),
        },
    )
    return result.to_dict()
