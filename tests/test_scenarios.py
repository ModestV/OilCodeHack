"""Decision invariants at horizon, evidence and hard constraint boundaries."""

import pytest
from pydantic import ValidationError

from backend import agents, scenarios
from backend.scenarios import ScenarioRequest, calculate_scenario


AT = "2025-01-01T00:00:00"


@pytest.fixture
def evidence(monkeypatch):
    values = [
        {"metric_id": metric, "value": value, "timestamp": AT,
         "available_at": AT, "freshness": "fresh", "flags": []}
        for metric, value in (("ht.T6", 300), ("ht.F9", 210), ("ht.P13", 5),
                              ("pak.ht.Mg.Sulfur", 8))
    ]
    monkeypatch.setattr(agents, "snapshot", lambda *args, **kwargs: {"values": values})
    monkeypatch.setattr(scenarios, "snapshot", lambda *args, **kwargs: {"values": values})
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: {
        "status": "ok", "prediction_ridge": 8, "alarm_above_10": False,
    })
    return values


def decide(tmp_path, **kwargs):
    kwargs.setdefault("optimize_economics", False)
    return agents.make_decision(tmp_path, ScenarioRequest(at=AT, **kwargs))


def test_horizon_result_matches_trajectory_not_steady_state(tmp_path, evidence):
    result = calculate_scenario(tmp_path, ScenarioRequest(
        at=AT, current_sulfur=10.5, horizon_minutes=30,
        changes={"temperature": 10},
    ))
    assert result["steady_state_sulfur"] == pytest.approx(9.7)
    assert result["predicted_sulfur"] == pytest.approx(10.5 - .8 / 3)
    assert result["predicted_sulfur"] == result["trajectory"][-1]["sulfur"]
    assert result["sulfur_target_met"] is False


@pytest.mark.parametrize("lag", [0, 90])
def test_zero_horizon_never_credits_future_intervention(tmp_path, evidence, lag):
    result = calculate_scenario(tmp_path, ScenarioRequest(
        at=AT, current_sulfur=10.5, horizon_minutes=0,
        parameters={"lag_minutes": lag}, changes={"temperature": 10},
    ))
    assert result["predicted_sulfur"] == 10.5
    assert result["sulfur_target_met"] is False


def test_automatic_changes_solve_requested_horizon(tmp_path, evidence):
    result = calculate_scenario(tmp_path, ScenarioRequest(
        at=AT, current_sulfur=10.5, horizon_minutes=45, step_minutes=15,
    ))
    assert result["predicted_sulfur"] == pytest.approx(10)
    assert result["controls"]["ht.T6"]["change"] == 10
    assert result["steady_state_sulfur"] == pytest.approx(9.5)


def test_all_infeasible_abstains_and_retains_diagnostics(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=12)
    assert result["status"] == "abstain"
    assert result["recommendation"] is result["scenario"] is result["selected_candidate"] is None
    assert len(result["candidates"]) == 3
    assert all(not candidate["feasible"] for candidate in result["candidates"])
    assert result["safety_gate"]["passed"] is False
    assert result["safety_gate"]["reasons"]


def test_feasible_choice_minimizes_effort_and_retains_manual_input(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=8, changes={"temperature": 10})
    assert result["status"] == "recommendation"
    assert result["selected_candidate"] == "hold"
    requested = next(item for item in result["candidates"] if item["id"] == "requested")
    assert requested["controls"]["ht.T6"]["change"] == 10
    assert requested["predicted_sulfur"] == pytest.approx(7.2)
    assert result["recommendation"]["action"] == "review_controls"
    assert result["basis"] == "scenario_only"
    assert result["operational_safety_validated"] is False
    assert result["safety_gate"]["unassessed"] == ["t95", "cetane"]


def test_requested_candidate_can_win_when_feasible_with_less_effort(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=10.4,
                    parameters={"pressure_effect": -1}, changes={"pressure": .4})
    assert result["selected_candidate"] == "requested"
    assert result["recommendation"]["predicted_sulfur"] == pytest.approx(10)


def test_editable_target_cannot_relax_ten_mg_limit(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=12, targets={"sulfur_max": 100})
    assert result["status"] == "abstain"
    assert all(item["target_met"] for item in result["candidates"])
    assert all(not item["safety_gate"]["passed"] for item in result["candidates"])
    assert "10 мг/кг" in " ".join(result["safety_gate"]["reasons"])


@pytest.mark.parametrize("field,value", [("sulfur", 11), ("t95", 370), ("cetane", 49)])
def test_any_blend_quality_violation_blocks_decision(tmp_path, evidence, field, value):
    tank = {"name": "A", "share": 100, "sulfur": 8, "t95": 350, "cetane": 52}
    tank[field] = value
    result = decide(tmp_path, current_sulfur=8, tanks=[tank])
    assert result["status"] == "abstain"
    assert all(not item["feasible"] for item in result["candidates"])


@pytest.mark.parametrize("field,value", [
    ("freshness", "stale"), ("flags", ["flatline"]), ("flags", ["suspect"]),
    ("value", None), ("available_at", "2025-01-01T00:01:00"),
])
def test_each_control_requires_fresh_unflagged_available_evidence(tmp_path, evidence, field, value):
    evidence[0][field] = value
    result = decide(tmp_path, current_sulfur=8)
    assert result["status"] == "abstain"
    assert len(result["candidates"]) == 3
    assert result["recommendation"] is None
    assert "ht.T6" in " ".join(result["safety_gate"]["reasons"])


def test_stale_quality_is_hard_gate_even_with_good_controls(tmp_path, evidence):
    evidence[-1]["freshness"] = "stale"
    result = decide(tmp_path)
    assert result["status"] == "abstain"
    assert result["agents"]["reliability"]["can_recommend"] is False


def test_normal_observed_decision_has_forecast_basis_and_requires_review(tmp_path, evidence):
    result = decide(tmp_path, current_t95=350, current_cetane=52)
    assert result["status"] == "recommendation"
    assert result["basis"] == "observed_and_forecast"
    assert result["selected_candidate"] == "hold"
    assert result["recommendation"]["requires_operator_review"] is True
    assert result["safety_gate"]["operational_safety_validated"] is False


def test_missing_other_quality_blocks_observed_recommendation(tmp_path, evidence):
    result = decide(tmp_path)
    assert result["status"] == "abstain"
    assert result["recommendation"] is None
    assert "Нет обязательного показателя качества" in " ".join(result["safety_gate"]["reasons"])


@pytest.mark.parametrize("quality", [{"current_t95": 370}, {"current_cetane": 49}])
def test_sulfur_target_cannot_override_known_other_quality_failure(tmp_path, evidence, quality):
    result = decide(tmp_path, current_sulfur=8, **quality)
    assert result["status"] == "abstain"
    assert all(item["target_met"] for item in result["candidates"])
    assert result["safety_gate"]["passed"] is False


def test_invalid_recipe_retains_each_candidate_error(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=8, tanks=[
        {"name": "A", "share": 90, "sulfur": 8, "t95": 350, "cetane": 52},
    ])
    assert result["status"] == "abstain"
    assert len(result["candidates"]) == 1  # Stored-only blend has no linked reactor intervention.
    assert all(item["status"] == "error" for item in result["candidates"])
    assert "100%" in result["safety_gate"]["reasons"][0]


def test_forecast_abstain_blocks_observed_decision_but_allows_labeled_what_if(tmp_path, evidence, monkeypatch):
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: {
        "status": "abstain", "reasons": ["Недостаточное покрытие признаков"],
        "prediction_ridge": None, "alarm_above_10": None,
    })
    observed = decide(tmp_path)
    assert observed["status"] == "abstain"
    assert observed["basis"] == "observed_and_forecast"
    hypothetical = decide(tmp_path, current_sulfur=8)
    assert hypothetical["status"] == "recommendation"
    assert hypothetical["basis"] == "scenario_only"


def test_forecast_alarm_not_claimed_resolved_by_causal_surrogate(tmp_path, evidence, monkeypatch):
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: {
        "status": "ok", "prediction_ridge": 11, "alarm_above_10": True,
    })
    assert decide(tmp_path)["status"] == "abstain"
    hypothetical = decide(tmp_path, current_sulfur=8)
    assert hypothetical["safety_gate"]["forecast_alarm"] is True
    assert hypothetical["operational_safety_validated"] is False


@pytest.mark.parametrize("kwargs", [
    {"current_sulfur": float("inf")}, {"targets": {"sulfur_max": float("inf")}},
    {"parameters": {"temperature_effect": float("-inf")}},
])
def test_nonfinite_scenario_values_rejected(kwargs):
    with pytest.raises(ValidationError):
        ScenarioRequest(at=AT, **kwargs)


def test_candidate_objectives_report_physical_tradeoffs(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=8, changes={"temperature": 5, "pressure": .2, "feed_rate_pct": -5})
    requested = next(c for c in result["candidates"] if c["id"] == "requested")
    objectives = requested["objectives"]
    assert objectives["throughput_index"] == pytest.approx(.95)
    assert objectives["energy_cost_index"] == pytest.approx(1.005)
    assert objectives["regime_severity"]["failure_probability"] is None
    feasible = [c for c in result["candidates"] if c["feasible"]]
    assert result["selected_candidate"] == min(feasible, key=lambda c: (c["objectives"]["ranking_loss"], c["effort"], c["predicted_sulfur"]))["id"]


def test_regime_model_cap_cannot_be_offset_by_quality_or_economics(tmp_path, evidence):
    # Pressure far beyond training support; quality alone remains satisfactory.
    evidence[2]["value"] = 20
    result = decide(tmp_path, current_sulfur=8)
    assert result["status"] == "abstain"
    assert all(c["target_met"] for c in result["candidates"])
    assert all(not c["feasible"] for c in result["candidates"])
    assert "экспериментальный предел" in " ".join(result["safety_gate"]["reasons"])


@pytest.mark.parametrize("quality,targets", [
    ({"current_t95": 370}, {"t95_max": 400}),
    ({"current_cetane": 49}, {"cetane_min": 40}),
])
def test_updated_scheme_quality_limits_cannot_be_relaxed(tmp_path, evidence, quality, targets):
    result = decide(tmp_path, current_sulfur=8, targets=targets, **quality)
    assert result["status"] == "abstain"
    assert all(not c["feasible"] for c in result["candidates"])
