from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


BENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH_ROOT))

from agents import safety_gate, scenario_agent  # noqa: E402
from agents.orchestrator import load_snapshot, run  # noqa: E402


class StressMatrixTests(unittest.TestCase):
    def normal_state(self) -> dict:
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

    def test_stale_lims_uses_fresh_pak_and_marks_data_limited(self) -> None:
        state = self.normal_state()
        state["latest_lims"]["Mg.Sulfur"]["age_hours"] = 13.0
        result, agents = self.run_case(state)

        self.assertEqual(result["recommendation_packet"]["decision"], "recommend")
        self.assertEqual(agents["data_quality"]["data_status"], "limited")
        self.assertTrue(any("ЛИМС устарел" in item for item in agents["data_quality"]["anomalies"]))
        self.assertEqual(agents["data_quality"]["source_priority_used"]["sulfur"], "PAK")

    def test_missing_pak_uses_fresh_lims_and_keeps_recommendation_available(self) -> None:
        state = self.normal_state()
        state["latest_pak"] = {}
        result, agents = self.run_case(state)

        self.assertEqual(result["recommendation_packet"]["decision"], "recommend")
        self.assertEqual(agents["data_quality"]["data_status"], "limited")
        self.assertIn("ПАК недоступен для выбранного среза", agents["data_quality"]["anomalies"])
        self.assertEqual(agents["data_quality"]["source_priority_used"]["sulfur"], "LIMS")

    def test_no_direct_quality_source_refuses_operator_recommendation(self) -> None:
        state = self.normal_state()
        state["latest_lims"] = {}
        state["latest_pak"] = {}
        result, agents = self.run_case(state)

        self.assertEqual(result["recommendation_packet"]["decision"], "reject")
        self.assertIn("безопасных сценариев", result["recommendation_packet"]["reason"])
        rejection_reasons = [
            reason
            for item in agents["safety_gate"]["rejected_scenarios"]
            for reason in item["reasons"]
        ]
        self.assertTrue(any("scenario confidence < 0.50" in reason for reason in rejection_reasons))

    def test_safety_gate_rejects_sulfur_above_limit(self) -> None:
        result = safety_gate.run(
            {
                "agent_results": {
                    "data_quality": {"can_recommend": True, "forbidden_changes": []},
                    "reliability_state": {"forbidden_changes": []},
                    "candidate_scenarios": {
                        "candidate_scenarios": [
                            {
                                "scenario_id": "unsafe-sulfur",
                                "changes": [],
                                "expected_effect": {
                                    "sulfur_mg_kg": 10.1,
                                    "reliability_risk_score": 0.2,
                                },
                                "confidence": 0.9,
                            }
                        ]
                    },
                }
            }
        )["output"]

        self.assertFalse(result["can_issue_recommendation"])
        self.assertIn("forecast_sulfur_mg_kg > 10", result["rejected_scenarios"][0]["reasons"])

    def test_safety_gate_rejects_uncontrolled_or_out_of_range_action(self) -> None:
        result = safety_gate.run(
            {
                "agent_results": {
                    "data_quality": {"can_recommend": True, "forbidden_changes": []},
                    "reliability_state": {"forbidden_changes": []},
                    "candidate_scenarios": {
                        "candidate_scenarios": [
                            {
                                "scenario_id": "unsafe-control",
                                "changes": [
                                    {"parameter": "T5", "current": 360, "proposed": 380},
                                    {"parameter": "UNKNOWN", "current": 1, "proposed": 2},
                                ],
                                "expected_effect": {
                                    "sulfur_mg_kg": 8.0,
                                    "reliability_risk_score": 0.2,
                                },
                                "confidence": 0.9,
                            }
                        ]
                    },
                }
            }
        )["output"]

        reasons = result["rejected_scenarios"][0]["reasons"]
        self.assertIn("T5 proposed value outside model range", reasons)
        self.assertIn("parameter UNKNOWN is not controlled", reasons)

    def test_scenario_agent_returns_hold_when_no_control_is_available(self) -> None:
        result = scenario_agent.run(
            {
                "unit_242000_telemetry": {},
                "avt_telemetry": {},
                "agent_results": {
                    "data_quality": {"confidence": 0.9, "data_status": "healthy"},
                    "quality_state": {
                        "confidence": 0.9,
                        "forecast_quality": {"sulfur_mg_kg": 8.0},
                        "model": {"coefficients": {}},
                    },
                    "reliability_state": {
                        "confidence": 0.9,
                        "risk_score": 0.2,
                        "risk_components": {},
                        "forbidden_changes": [],
                    },
                },
            }
        )["output"]["candidate_scenarios"]

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["scenario_id"], "S0")
        self.assertEqual(result[0]["changes"], [])

    def test_low_confidence_candidate_is_rejected_even_with_safe_numeric_effect(self) -> None:
        result = safety_gate.run(
            {
                "agent_results": {
                    "data_quality": {"can_recommend": True, "forbidden_changes": []},
                    "reliability_state": {"forbidden_changes": []},
                    "candidate_scenarios": {
                        "candidate_scenarios": [
                            {
                                "scenario_id": "low-confidence",
                                "changes": [],
                                "expected_effect": {
                                    "sulfur_mg_kg": 8.0,
                                    "reliability_risk_score": 0.2,
                                },
                                "confidence": 0.49,
                            }
                        ]
                    },
                }
            }
        )["output"]

        self.assertFalse(result["can_issue_recommendation"])
        self.assertIn(
            "scenario confidence < 0.50 or unavailable",
            result["rejected_scenarios"][0]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
