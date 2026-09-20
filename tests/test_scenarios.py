"""Decision invariants at horizon, evidence and hard constraint boundaries."""

import math

import pytest
from pydantic import ValidationError

from backend import agents, scenarios
from backend.scenarios import ScenarioRequest, calculate_scenario


AT = "2025-01-01T00:00:00"
# Explicit surrogate coefficients so the tests do not depend on the shipped artifact.
PARAMS = {"lag_minutes": 90, "temperature_effect": -0.04, "feed_rate_effect": 0.02, "pressure_effect": -0.2}


@pytest.fixture
def evidence(monkeypatch):
    values = [
        {"metric_id": metric, "value": value, "timestamp": AT,
         "available_at": AT, "freshness": "fresh", "flags": [], "age_minutes": 0.0}
        for metric, value in (("ht.T6", 350), ("ht.F9", 200), ("ht.P13", 3.9),
                              ("pak.ht.Mg.Sulfur", 8))
    ]
    monkeypatch.setattr(agents, "snapshot", lambda *args: {"values": values})
    monkeypatch.setattr(scenarios, "snapshot", lambda *args: {"values": values})
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: {
        "status": "ok", "prediction": 8, "alarm_above_10": False, "exceedance_probability": 0.1, "alarm_probability": 0.3,
    })
    return values


def forecast_with_nowcast(value, exceedance=0.1):
    """A forecast object as the runtime returns it, with a flat no-action path."""
    return {"status": "ok", "prediction": value, "alarm_above_10": exceedance >= 0.3, "alarm_probability": 0.3,
            "exceedance_probability": exceedance, "feature_time": AT,
            "nowcast": {"prediction": value, "lower": value * 0.8, "upper": value * 1.2, "exceedance_probability": exceedance},
            "horizons": [{"minutes": h, "prediction": value} for h in (0, 60, 120, 180)]}


def decide(tmp_path, **kwargs):
    kwargs.setdefault("parameters", PARAMS)
    return agents.make_decision(tmp_path, ScenarioRequest(at=AT, **kwargs))


def scenario(tmp_path, **kwargs):
    kwargs.setdefault("parameters", PARAMS)
    return calculate_scenario(tmp_path, ScenarioRequest(at=AT, **kwargs))


def test_horizon_result_matches_trajectory_not_steady_state(tmp_path, evidence):
    result = scenario(tmp_path, current_sulfur=10.5, horizon_minutes=30, changes={"temperature": 10})
    assert result["steady_state_sulfur"] == pytest.approx(10.5 * math.exp(-0.4))
    assert result["predicted_sulfur"] == pytest.approx(10.5 * math.exp(-0.4 / 3))
    assert result["predicted_sulfur"] == result["trajectory"][-1]["sulfur"]
    assert result["sulfur_target_met"] is False
    assert result["baseline"]["sulfur_source"] == "request.current_sulfur"


@pytest.mark.parametrize("horizon", [0, 30, 180])
def test_feed_response_point_and_risk_use_same_horizon(tmp_path, evidence, monkeypatch, horizon):
    from backend.forecast import exceedance_at, _load_artifact
    model = _load_artifact()
    result = scenario(tmp_path, current_sulfur=8, baseline_feed_sulfur=1, feed_sulfur=2,
                      changes={}, horizon_minutes=horizon)
    expected = exceedance_at(model, horizon, math.log(result["predicted_sulfur"]))
    assert result["predicted_sulfur_lower"] == pytest.approx(expected["lower"])
    assert result["predicted_sulfur_upper"] == pytest.approx(expected["upper"])
    assert result["exceedance_probability"] == pytest.approx(expected["exceedance_probability"])


def test_short_horizon_automatic_control_accounts_for_feed_ramp(tmp_path, evidence):
    result = scenario(tmp_path, current_sulfur=8.5, baseline_feed_sulfur=1, feed_sulfur=1.2,
                      horizon_minutes=45, step_minutes=15)
    assert result["predicted_sulfur"] == pytest.approx(9.)


def test_unsupported_intermediate_forecast_blocks_observed_decision(tmp_path, evidence, monkeypatch):
    forecast = {**forecast_with_nowcast(8), "path_supported": False}
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: forecast)
    result = decide(tmp_path, current_t95=350, current_cetane=52)
    assert result["status"] == "abstain"


@pytest.mark.parametrize("lag", [0, 90])
def test_zero_horizon_never_credits_future_intervention(tmp_path, evidence, lag):
    result = scenario(tmp_path, current_sulfur=10.5, horizon_minutes=0,
                      parameters={**PARAMS, "lag_minutes": lag}, changes={"temperature": 10})
    assert result["predicted_sulfur"] == 10.5
    assert result["sulfur_target_met"] is False


def test_automatic_changes_solve_requested_horizon_to_editable_target(tmp_path, evidence):
    result = scenario(tmp_path, current_sulfur=10.5, horizon_minutes=45, step_minutes=15)
    # Default target is 9 mg/kg (1 mg/kg expert margin); response at 45/90 min is 0.5.
    assert result["predicted_sulfur"] == pytest.approx(9.0)
    assert result["controls"]["ht.T6"]["change"] == pytest.approx(math.log(10.5 / 9) / 0.5 / 0.04)
    assert result["controls"]["ht.P13"]["change"] == 0
    assert result["steady_state_sulfur"] < 9.0


def test_automatic_changes_escalate_to_pressure_and_feed_after_temperature_limit(tmp_path, evidence):
    result = scenario(tmp_path, current_sulfur=20, horizon_minutes=180)
    assert result["controls"]["ht.T6"]["change"] == 10
    assert result["controls"]["ht.P13"]["change"] == pytest.approx(0.5)
    assert result["controls"]["ht.F9"]["change"] == -10
    # Every control is at its model limit and the target is still unreachable.
    assert result["predicted_sulfur"] == pytest.approx(20 * math.exp(-0.4 - 0.1 - 0.2))
    assert result["sulfur_target_met"] is False
    moderate = scenario(tmp_path, current_sulfur=15, horizon_minutes=180)
    assert moderate["controls"]["ht.T6"]["change"] == 10
    assert 0 < moderate["controls"]["ht.P13"]["change"] <= 0.5
    assert moderate["predicted_sulfur"] == pytest.approx(9.0)


def test_feed_sulfur_increase_raises_product_and_triggers_temperature_response(tmp_path, evidence):
    hold = scenario(tmp_path, current_sulfur=8.5, baseline_feed_sulfur=0.9, feed_sulfur=1.1, changes={})
    assert hold["predicted_sulfur"] == pytest.approx(8.5 * 1.1 / 0.9)
    automatic = scenario(tmp_path, current_sulfur=8.5, baseline_feed_sulfur=0.9, feed_sulfur=1.1)
    assert automatic["controls"]["ht.T6"]["change"] > 0
    assert automatic["predicted_sulfur"] == pytest.approx(9.0)
    assert scenario(tmp_path, current_sulfur=8.5, baseline_feed_sulfur=0.9, feed_sulfur=0.9,
                    changes={})["predicted_sulfur"] == pytest.approx(8.5)


def test_all_infeasible_abstains_and_retains_diagnostics(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=25)
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
    assert requested["predicted_sulfur"] == pytest.approx(8 * math.exp(-0.4))
    assert result["recommendation"]["action"] == "review_controls"
    assert result["basis"] == "scenario_only"
    assert result["operational_safety_validated"] is False
    assert result["safety_gate"]["unassessed"] == ["t95", "cetane"]


def test_requested_candidate_can_win_when_feasible_with_less_effort(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=10.4, parameters={**PARAMS, "pressure_effect": -1.5}, changes={"pressure": .15})
    assert result["selected_candidate"] == "requested"
    assert result["recommendation"]["predicted_sulfur"] == pytest.approx(10.4 * math.exp(-0.225))


def test_editable_target_cannot_relax_ten_mg_limit(tmp_path, evidence):
    result = decide(tmp_path, current_sulfur=12, targets={"sulfur_max": 100})
    hold = next(item for item in result["candidates"] if item["id"] == "hold")
    assert hold["feasible"] is False and hold["target_met"] is True
    assert "10 мг/кг" in " ".join(hold["safety_gate"]["reasons"])
    assert all(item["predicted_sulfur"] <= 10 + 1e-9 for item in result["candidates"] if item["feasible"])


def test_exceedance_probability_gate_blocks_risky_hold(tmp_path, evidence, monkeypatch):
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: forecast_with_nowcast(9.6, exceedance=0.45))
    monkeypatch.setattr(scenarios, "exceedance_at", lambda artifact, horizon, ln_pred, limit=None: {
        "prediction": math.exp(ln_pred), "lower": math.exp(ln_pred) * 0.8, "upper": math.exp(ln_pred) * 1.2,
        "exceedance_probability": 0.45 if math.exp(ln_pred) > 9.5 else 0.15, "limit": 10.0, "interval": "test"})
    monkeypatch.setattr(scenarios, "_load_artifact", lambda *args, **kwargs: {})
    result = decide(tmp_path, current_t95=350, current_cetane=52)
    assert result["agents"]["quality"]["evidence"]["sulfur"]["source"] == "model.nowcast"
    hold = next(item for item in result["candidates"] if item["id"] == "hold")
    assert hold["feasible"] is False
    assert "Вероятность превышения" in " ".join(hold["safety_gate"]["reasons"])
    assert result["status"] == "recommendation"
    assert result["recommendation"]["controls"]["ht.T6"]["change"] > 0
    assert result["recommendation"]["exceedance_probability"] == pytest.approx(0.15)


@pytest.mark.parametrize("field,value", [("sulfur", 11), ("t95", 370), ("cetane", 49)])
def test_any_blend_quality_violation_blocks_decision(tmp_path, evidence, field, value):
    tank = {"name": "A", "share": 100, "sulfur": 8, "t95": 350, "cetane": 52}
    tank[field] = value
    result = decide(tmp_path, current_sulfur=8, tanks=[tank])
    assert result["status"] == "abstain"
    assert all(not item["feasible"] for item in result["candidates"])


def test_additive_dilutes_sulfur_and_raises_cetane_without_changing_t95(tmp_path, evidence):
    result = scenario(tmp_path, current_sulfur=8, additive_pct=2,
                      tanks=[{"name": "A", "share": 100, "sulfur": 9, "t95": 350, "cetane": 49}])
    assert result["blend"]["sulfur"] == pytest.approx(9 * 0.98)
    assert result["blend"]["cetane"] == pytest.approx(49 + 8)
    assert result["blend"]["t95"] == 350
    assert result["blend"]["cost_index"] == pytest.approx(0.98 + 2)


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


def test_month_old_cetane_does_not_become_valid_via_slow_quality_override(tmp_path, evidence):
    evidence.append({"metric_id": "lims.ht.2.CetaneNumber", "value": 52, "timestamp": "2024-12-01T10:00:00",
                     "available_at": "2024-12-01T14:00:00", "freshness": "stale", "flags": [], "age_minutes": 31 * 24 * 60.0})
    result = decide(tmp_path, current_t95=350)
    assert result["status"] == "abstain"
    assert result["candidates"]
    for candidate in result["candidates"]:
        checks = {c["name"]: c for c in candidate["safety_gate"]["checks"]}
        assert checks["cetane_evidence"]["passed"] is False
    assert agents.SLOW_QUALITY_MAX_AGE_MINUTES["lims.ht.2.CetaneNumber"] == 48 * 60


def test_stale_t95_falls_back_to_vak_virtual_analyser(tmp_path, evidence, monkeypatch):
    evidence.append({"metric_id": "lims.ht.2.95%.T", "value": 350, "timestamp": "2024-12-20T10:00:00",
                     "available_at": "2024-12-20T14:00:00", "freshness": "stale", "flags": [], "age_minutes": 12 * 24 * 60.0})
    monkeypatch.setattr(agents, "formula_results", lambda *args: {"formulas": [
        {"id": "24-2000:GODT:T95", "result": 352.5, "status": "experimental",
         "inputs": [{"tag": "T6", "freshness": "fresh", "flags": []}]}]})
    result = decide(tmp_path, current_cetane=52)
    quality = result["agents"]["quality"]["evidence"]["other_quality"]
    assert quality["t95"]["source"] == "vak.24-2000:GODT:T95" and quality["t95"]["value"] == 352.5
    assert result["status"] == "recommendation"
    monkeypatch.setattr(agents, "formula_results", lambda *args: {"formulas": [
        {"id": "24-2000:GODT:T95", "result": 365.0, "status": "experimental",
         "inputs": [{"tag": "T6", "freshness": "fresh", "flags": []}]}]})
    assert decide(tmp_path, current_cetane=52)["status"] == "abstain"


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
    assert len(result["candidates"]) == 3
    assert all(item["status"] == "error" for item in result["candidates"])
    assert "100%" in result["safety_gate"]["reasons"][0]


def test_forecast_abstain_blocks_observed_decision_but_allows_labeled_what_if(tmp_path, evidence, monkeypatch):
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: {
        "status": "abstain", "reasons": ["Недостаточное покрытие признаков"],
        "prediction": None, "alarm_above_10": None,
    })
    observed = decide(tmp_path)
    assert observed["status"] == "abstain"
    assert observed["basis"] == "observed_and_forecast"
    hypothetical = decide(tmp_path, current_sulfur=8)
    assert hypothetical["status"] == "recommendation"
    assert hypothetical["basis"] == "scenario_only"


def test_forecast_alarm_triggers_corrective_candidate_instead_of_blocking(tmp_path, evidence, monkeypatch):
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *args, **kwargs: forecast_with_nowcast(11, exceedance=0.6))
    monkeypatch.setattr(scenarios, "_load_artifact", lambda *args, **kwargs: (_ for _ in ()).throw(scenarios.ForecastUnavailable("x")))
    result = decide(tmp_path, current_t95=350, current_cetane=52)
    assert result["agents"]["quality"]["evidence"]["sulfur"]["value"] == 11
    hold = next(item for item in result["candidates"] if item["id"] == "hold")
    assert hold["feasible"] is False
    assert result["status"] == "recommendation"
    assert result["selected_candidate"] in {"automatic", "conservative"}
    assert result["recommendation"]["controls"]["ht.T6"]["change"] > 0
    assert result["safety_gate"]["forecast_alarm"] is True
    assert result["operational_safety_validated"] is False


@pytest.mark.parametrize("kwargs", [
    {"current_sulfur": float("inf")}, {"targets": {"sulfur_max": float("inf")}},
    {"parameters": {"temperature_effect": float("-inf")}}, {"changes": {"pressure": 1.0}},
    {"targets": {"max_exceedance_probability": 1.5}},
])
def test_invalid_scenario_values_rejected(kwargs):
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


def test_scenario_defaults_come_from_step_response_evidence(tmp_path, evidence):
    defaults = scenarios.scenario_defaults()
    assert defaults["temperature_effect"] < 0 < defaults["feed_rate_effect"]
    assert defaults["pressure_effect"] < 0
    assert 0 <= defaults["lag_minutes"] <= 180
    result = calculate_scenario(tmp_path, ScenarioRequest(at=AT, current_sulfur=8))
    assert result["parameters"]["temperature_effect"] == defaults["temperature_effect"]
    assert result["parameter_defaults"]["basis"]
