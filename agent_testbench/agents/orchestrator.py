from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import dq_agent, quality_agent, reliability_agent, safety_gate, scenario_agent
from .blackboard import Blackboard
from .common import SULFUR_LIMIT_MG_KG, clone_state, confidence_label


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "databases" / "oilcode_agent_test.db"


def load_snapshot(snapshot_id: str, db_path: Path = DB_PATH) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select process_state_json from process_state_snapshots where snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    if row is None:
        raise KeyError(f"snapshot_id not found: {snapshot_id}")
    return json.loads(row[0])


def list_snapshots(db_path: Path = DB_PATH) -> list[dict[str, str]]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select snapshot_id, timestamp from process_state_snapshots order by timestamp"
        ).fetchall()
    return [{"snapshot_id": row[0], "timestamp": row[1]} for row in rows]


def _rank_scenarios(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> tuple[float, float, float, float, float]:
        effect = item.get("expected_effect", {})
        sulfur = float(effect.get("sulfur_mg_kg") or SULFUR_LIMIT_MG_KG)
        risk = float(effect.get("reliability_risk_score") or 1.0)
        production = float(effect.get("production_proxy") or 0.0)
        energy = float(effect.get("energy_proxy") or 0.0)
        confidence = float(item.get("confidence") or 0.0)
        return (sulfur, risk, -production, energy, -confidence)

    ranked = []
    for rank, scenario in enumerate(sorted(accepted, key=score), start=1):
        item = clone_state(scenario)
        item["rank"] = rank
        ranked.append(item)

    selected = ranked[0] if ranked else None
    return {
        "ranked_scenarios": ranked,
        "selected_scenario": None if selected is None else selected["scenario_id"],
        "selection_reason": (
            "нет сценариев после Safety Gate"
            if selected is None
            else "выбран сценарий с лучшим балансом серы, риска режима, выпуска, энергии и confidence"
        ),
    }


def _format_action(scenario: dict[str, Any] | None) -> str:
    if not scenario:
        return "нет действия"
    changes = []
    for change in scenario.get("changes", []):
        current = float(change["current"])
        proposed = float(change["proposed"])
        direction = "снизить" if proposed < current else "повысить" if proposed > current else "оставить"
        delta = abs(proposed - current)
        changes.append(f"{direction} {change['parameter']} на {delta:.2f} {change.get('unit', '')}".strip())
    return ", ".join(changes) if changes else "оставить режим без изменений"


def _recommendation_packet(
    state: dict[str, Any],
    ranking: dict[str, Any],
    safety: dict[str, Any],
) -> dict[str, Any]:
    selected = None
    for scenario in ranking.get("ranked_scenarios", []):
        if scenario["scenario_id"] == ranking.get("selected_scenario"):
            selected = scenario
            break

    if selected is None:
        return {
            "decision": "reject",
            "reason": "безопасных сценариев после Safety Gate не найдено",
            "rejected_scenarios": safety.get("rejected_scenarios", []),
        }

    confidence = min(
        float(state["agent_results"]["data_quality"].get("confidence", 0.0)),
        float(selected.get("confidence", 0.0)),
        float(state["agent_results"]["quality_state"].get("confidence", 0.0)),
        float(state["agent_results"]["reliability_state"].get("confidence", 0.0)),
    )
    effect = selected.get("expected_effect", {})
    return {
        "decision": "recommend",
        "selected_scenario": selected["scenario_id"],
        "action": _format_action(selected),
        "expected_quality": {
            "sulfur_mg_kg": effect.get("sulfur_mg_kg"),
            "limit": SULFUR_LIMIT_MG_KG,
        },
        "reliability": {
            "risk_score": effect.get("reliability_risk_score"),
            "risk_class": state["agent_results"]["reliability_state"].get("risk_class"),
        },
        "confidence": confidence_label(confidence),
        "confidence_value": round(confidence, 3),
        "confidence_reason": "confidence ограничен самым слабым из DQ, Quality, Reliability и сценария",
        "rejected_scenarios": safety.get("rejected_scenarios", []),
        "alternatives": [
            {
                "scenario_id": scenario["scenario_id"],
                "tradeoff": "альтернатива после Safety Gate, но ниже в ranking",
            }
            for scenario in ranking.get("ranked_scenarios", [])[1:]
        ],
    }


def _explain(packet: dict[str, Any]) -> str:
    if packet.get("decision") != "recommend":
        return f"Надёжной рекомендации нет: {packet.get('reason', 'причина не указана')}."

    sulfur = packet["expected_quality"]["sulfur_mg_kg"]
    limit = packet["expected_quality"]["limit"]
    risk = packet["reliability"]["risk_score"]
    return (
        f"Рекомендуется сценарий {packet['selected_scenario']}: {packet['action']}. "
        f"Прогноз серы {sulfur} mg/kg при лимите {limit} mg/kg. "
        f"Ожидаемый риск режима {risk}. "
        f"Уверенность: {packet['confidence']} ({packet['confidence_value']})."
    )


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    board = Blackboard(process_state)
    board.add_step(
        "ProcessState received",
        {
            "step": "load_process_state",
            "status": "completed",
            "reason": "оркестратор получил исходный срез и создал Blackboard",
        },
    )

    dq = dq_agent.run(board.snapshot())
    board.add_agent_result("data_quality", dq)

    if not dq["output"]["can_continue"]:
        packet = {
            "decision": "reject",
            "reason": "; ".join(dq["output"]["blocking_reasons"]),
            "rejected_scenarios": [],
        }
        board.add_step("Pipeline stopped", packet)
        return {
            "snapshot_id": process_state.get("snapshot_id"),
            "timestamp": process_state.get("timestamp"),
            "trace": board.trace,
            "final_state": board.snapshot(),
            "recommendation_packet": packet,
            "operator_message": _explain(packet),
        }

    quality = quality_agent.run(board.snapshot())
    board.add_agent_result("quality_state", quality)

    reliability = reliability_agent.run(board.snapshot())
    board.add_agent_result("reliability_state", reliability)

    scenarios = scenario_agent.run(board.snapshot())
    board.add_agent_result("candidate_scenarios", scenarios)

    safety = safety_gate.run(board.snapshot())
    board.add_agent_result("safety_gate", safety)

    ranking = _rank_scenarios(safety["output"]["accepted_scenarios"])
    board.state["agent_results"]["ranking"] = ranking
    board.add_step(
        "Pareto / Ranking",
        {
            "step": "ranking",
            "status": "completed",
            "reason": ranking["selection_reason"],
            **ranking,
        },
    )

    packet = _recommendation_packet(board.snapshot(), ranking, safety["output"])
    board.state["recommendation_packet"] = packet
    board.state["operator_message"] = _explain(packet)
    board.add_step("Recommendation Packet", packet)

    return {
        "snapshot_id": process_state.get("snapshot_id"),
        "timestamp": process_state.get("timestamp"),
        "trace": board.trace,
        "final_state": board.snapshot(),
        "recommendation_packet": packet,
        "operator_message": board.state["operator_message"],
    }


def run_snapshot(snapshot_id: str) -> dict[str, Any]:
    return run(load_snapshot(snapshot_id))
