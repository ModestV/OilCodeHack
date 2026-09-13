from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


BENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH_ROOT))

from agents.orchestrator import load_snapshot, run  # noqa: E402


DECISION_MATRIX = {
    "normal": {
        "decision": "recommend",
        "reason_contains": [],
        "forbidden_changes": [],
    },
    "missing_critical_tag": {
        "decision": "recommend",
        "reason_contains": ["F26"],
        "forbidden_changes": [("F26", "increase"), ("F26", "decrease")],
    },
    "flatline": {
        "decision": "recommend",
        "reason_contains": ["F9: flatline"],
        "forbidden_changes": [("F9", "increase"), ("F9", "decrease")],
    },
    "lims_pak_conflict": {
        "decision": "recommend",
        "reason_contains": ["ЛИМС и ПАК по сере расходятся"],
        "forbidden_changes": [],
    },
    "unsafe_scenario": {
        "decision": "recommend",
        "reason_contains": ["T5"],
        "forbidden_changes": [("T5", "increase")],
    },
    "full_refusal": {
        "decision": "reject",
        "reason_contains": ["нет критичных режимных тегов T5/P13"],
        "forbidden_changes": [("recommendation", "issue")],
    },
}


class DecisionMatrixTests(unittest.TestCase):
    def normal_state(self) -> dict:
        """Стабильный срез без искусственно внесённых аномалий из тестовой БД."""
        state = deepcopy(load_snapshot("PS-002"))
        state["timestamp"] = "2023-01-01T02:00:00"
        state["unit_242000_telemetry"].update(
            {"T5": 362.3, "P13": 3.75, "F26": 208.5, "F9": 177.1, "F15": 2828.7}
        )
        state["avt_telemetry"]["F30"] = 99.9
        state["latest_lims"]["Mg.Sulfur"] = {
            "value": 8.0,
            "unit": "mg/kg",
            "timestamp": "2023-01-01T01:30:00",
            "age_hours": 0.5,
        }
        return state

    def run_case(self, state: dict) -> tuple[dict, dict]:
        result = run(state)
        return result, result["final_state"]["agent_results"]

    def assert_forbidden_changes(
        self, actual: list[dict], expected: list[tuple[str, str]]
    ) -> None:
        actual_pairs = {(item["parameter"], item["direction"]) for item in actual}
        self.assertEqual(actual_pairs, set(expected))

    def test_normal_mode_recommends_without_prohibitions(self) -> None:
        result, agents = self.run_case(self.normal_state())

        self.assertEqual(result["recommendation_packet"]["decision"], DECISION_MATRIX["normal"]["decision"])
        self.assertEqual(agents["data_quality"]["data_status"], "healthy")
        self.assert_forbidden_changes(
            agents["data_quality"]["forbidden_changes"],
            DECISION_MATRIX["normal"]["forbidden_changes"],
        )
        self.assertEqual(agents["reliability_state"]["forbidden_changes"], [])

    def test_missing_critical_tag_limits_control_of_that_tag(self) -> None:
        state = self.normal_state()
        state["unit_242000_telemetry"]["F26"] = None
        result, agents = self.run_case(state)

        expected = DECISION_MATRIX["missing_critical_tag"]
        self.assertEqual(result["recommendation_packet"]["decision"], expected["decision"])
        self.assertIn("F26", agents["data_quality"]["missing_critical_tags"])
        self.assert_forbidden_changes(agents["data_quality"]["forbidden_changes"], expected["forbidden_changes"])

    def test_flatline_blocks_changes_to_untrusted_tag(self) -> None:
        result, agents = self.run_case(deepcopy(load_snapshot("PS-004")))

        expected = DECISION_MATRIX["flatline"]
        self.assertEqual(result["recommendation_packet"]["decision"], expected["decision"])
        self.assertTrue(
            any(reason in anomaly for anomaly in agents["data_quality"]["anomalies"] for reason in expected["reason_contains"])
        )
        self.assert_forbidden_changes(agents["data_quality"]["forbidden_changes"], expected["forbidden_changes"])
        rejected_reasons = [reason for item in agents["safety_gate"]["rejected_scenarios"] for reason in item["reasons"]]
        self.assertTrue(any("F9 change forbidden by safety policy" in reason for reason in rejected_reasons))

    def test_lims_pak_conflict_is_explained_but_not_silently_ignored(self) -> None:
        state = self.normal_state()
        state["latest_lims"]["Mg.Sulfur"]["value"] = 6.0
        state["latest_pak"]["24-2000:Mg.Sulfur"]["value"] = 9.0
        result, agents = self.run_case(state)

        expected = DECISION_MATRIX["lims_pak_conflict"]
        self.assertEqual(result["recommendation_packet"]["decision"], expected["decision"])
        self.assertTrue(
            any(reason in anomaly for anomaly in agents["data_quality"]["anomalies"] for reason in expected["reason_contains"])
        )
        self.assertEqual(agents["data_quality"]["source_priority_used"]["sulfur"], "LIMS")
        self.assert_forbidden_changes(agents["data_quality"]["forbidden_changes"], expected["forbidden_changes"])

    def test_unsafe_scenario_is_rejected_even_if_another_action_is_available(self) -> None:
        state = self.normal_state()
        state["unit_242000_telemetry"]["T5"] = 367.0
        result, agents = self.run_case(state)

        expected = DECISION_MATRIX["unsafe_scenario"]
        self.assertEqual(result["recommendation_packet"]["decision"], expected["decision"])
        self.assert_forbidden_changes(agents["reliability_state"]["forbidden_changes"], expected["forbidden_changes"])
        rejected_reasons = [reason for item in agents["safety_gate"]["rejected_scenarios"] for reason in item["reasons"]]
        self.assertTrue(any("T5 change forbidden by safety policy" in reason for reason in rejected_reasons))

    def test_missing_required_state_stops_pipeline_and_rejects_recommendation(self) -> None:
        state = self.normal_state()
        state["unit_242000_telemetry"]["T5"] = None
        state["unit_242000_telemetry"]["P13"] = None
        result, agents = self.run_case(state)

        expected = DECISION_MATRIX["full_refusal"]
        packet = result["recommendation_packet"]
        self.assertEqual(packet["decision"], expected["decision"])
        self.assertTrue(all(reason in packet["reason"] for reason in expected["reason_contains"]))
        self.assertIsNone(agents["candidate_scenarios"])
        self.assertNotIn("selected_scenario", packet)


if __name__ == "__main__":
    unittest.main()
