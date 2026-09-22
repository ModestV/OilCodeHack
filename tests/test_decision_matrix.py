"""Stress matrix for the full agent contour on degraded inputs.

Ported from the `agents_test` testbench idea (stale/missing/conflicting sources,
flatline and missing mandatory tags) and rewritten for the real agents with the
confirmed control tags T6/F9/P13.  Every degraded case must abstain with a
reason, never emit an action, and still produce a trace and an explanation.
"""

from copy import deepcopy

import pytest

from backend import agents, scenarios
from backend.forecast import ForecastUnavailable
from backend.scenarios import ScenarioRequest

AT = "2026-05-01T12:00:00"
BASE = {
    "ht.T6": 360.0, "ht.F9": 210.0, "ht.P13": 3.9,
    "pak.ht.Mg.Sulfur": 8.0, "ht.Q21": 8.4,
    "lims.ht.2.Mg.Sulfur": 8.2, "lims.ht.2.95%.T": 350.0, "lims.ht.2.CetaneNumber": 52.0,
}


def frame(**changes):
    values = []
    for metric_id, value in BASE.items():
        item = {"metric_id": metric_id, "value": value, "timestamp": AT, "available_at": AT,
                "flags": [], "freshness": "fresh"}
        values.append(item)
    by_id = {item["metric_id"]: item for item in values}
    for key, change in changes.items():
        metric_id = key.replace("__", ".")
        if change is None:
            values.remove(by_id[metric_id])
        else:
            by_id[metric_id].update(change)
    return {"values": values}


def forecast(values=(8.0, 8.0, 8.0, 8.0)):
    return {"status": "ok", "path_supported": True, "horizon_minutes": 180, "at": AT, "feature_time": AT,
            "prediction": values[3], "prediction_upper": values[3] + 1, "prediction_lower": values[3] - 1,
            "exceedance_probability": 0.05 if values[3] < 10 else 0.6,
            "nowcast": {"prediction": values[0]}, "alarm_above_10": values[3] > 10,
            "leakage_check": {"passed": True},
            "horizons": [{"minutes": h, "prediction": p, "upper": p + 1, "status": "ok"}
                         for h, p in zip((0, 60, 120, 180), values)]}


def decide(monkeypatch, tmp_path, snapshot, predicted=None, **request):
    def predict(*_args, **_kwargs):
        if isinstance(predicted, Exception):
            raise predicted
        return deepcopy(predicted or forecast())

    monkeypatch.setattr(agents, "snapshot", lambda *args: snapshot)
    monkeypatch.setattr(agents, "forecast_sulfur", predict)
    monkeypatch.setattr(scenarios, "forecast_sulfur", predict)
    return agents.make_decision(tmp_path, ScenarioRequest(at=AT, optimize_economics=False, **request))


def reasons(decision):
    return " | ".join(decision["agents"]["reliability"]["reasons"] + [decision["abstain"]["reason"]])


def test_normal_steady_period_recommends_without_unneeded_action(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame())
    assert decision["status"] == "recommendation"
    assert decision["selected_candidate"] == "hold"
    assert decision["explanation"]["action"] == ["Удержать текущий режим: изменений не требуется"]
    assert all(check["passed"] for check in decision["consistency"])


def test_stale_lims_with_fresh_analysers_keeps_recommendation_on_the_nowcast(monkeypatch, tmp_path):
    snapshot = frame(lims__ht__2__Mg__Sulfur={"freshness": "stale"})
    decision = decide(monkeypatch, tmp_path, snapshot)
    assert decision["status"] == "recommendation"
    assert decision["agents"]["quality"]["evidence"]["sulfur"]["source"] == "model.nowcast"


def test_missing_pak_keeps_recommendation_when_forecast_is_supported(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame(pak__ht__Mg__Sulfur=None))
    assert decision["status"] == "recommendation"
    assert decision["agents"]["quality"]["evidence"]["analyzer_comparison"]["difference_mgkg"] is None


DEGRADED = {
    "pak_vs_q21_conflict": ({"ht__Q21": {"value": 12.0}}, "анализаторы серы расходятся"),
    "flatline_f9": ({"ht__F9": {"flags": ["flatline"]}}, "ht.F9: недостоверные данные (flatline)"),
    "missing_p13": ({"ht__P13": None}, "ht.P13: отсутствует конечное численное значение"),
    "stale_t6": ({"ht__T6": {"freshness": "stale"}}, "ht.T6: измерение не свежее"),
    "future_t6": ({"ht__T6": {"available_at": "2026-05-01T12:10:00"}}, "ht.T6: измерение ещё не доступно"),
    "cetane_older_than_60_days": ({"lims__ht__2__CetaneNumber": {"freshness": "stale", "timestamp": "2026-02-20T12:00:00",
                                                                  "available_at": "2026-02-20T16:00:00"}},
                                  "cetane: проба старше 60 сут"),
    "missing_t95": ({"lims__ht__2__95%__T": None}, "Нет обязательного показателя качества t95"),
}


@pytest.mark.parametrize("case", sorted(DEGRADED))
def test_degraded_input_abstains_with_reason_and_no_action(monkeypatch, tmp_path, case):
    changes, expected = DEGRADED[case]
    decision = decide(monkeypatch, tmp_path, frame(**changes))
    assert decision["status"] == "abstain"
    assert decision["recommendation"] is None
    assert decision["selected_candidate"] is None
    assert expected in reasons(decision) + " | " + " | ".join(decision["safety_gate"]["reasons"])
    assert [step["role"] for step in decision["trace"]][-1] == "orchestrator"
    assert decision["explanation"]["action"] == "Рекомендация не выдана"


def test_analyser_conflict_is_reported_as_a_conflict(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame(ht__Q21={"value": 12.0}))
    assert [c["code"] for c in decision["conflicts"]] == ["analyser_disagreement"]


def test_no_quality_source_and_no_forecast_abstains(monkeypatch, tmp_path):
    snapshot = frame(lims__ht__2__Mg__Sulfur=None, pak__ht__Mg__Sulfur=None, ht__Q21=None)
    decision = decide(monkeypatch, tmp_path, snapshot, predicted=ForecastUnavailable("нет модели"))
    assert decision["status"] == "abstain"
    assert decision["abstain"]["missing"] == ["sulfur_baseline"]


def test_forecast_alarm_blocks_observed_recommendation(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame(), predicted=forecast((9.0, 9.8, 10.6, 11.2)))
    assert decision["status"] == "abstain"
    assert "превышение 10 мг/кг" in reasons(decision)


def test_unsupported_forecast_path_abstains(monkeypatch, tmp_path):
    predicted = forecast()
    predicted["path_supported"] = False
    decision = decide(monkeypatch, tmp_path, frame(), predicted=predicted)
    assert decision["status"] == "abstain"
    assert "области применимости" in reasons(decision)


# --- risk-aware contour: the recommendation exists when a safe option exists --------------

PROBABILITIES = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
# ln-residuals of a model with ~0.15 spread: P(S > 10) = 0.2 at S ≈ 8.8.
RESIDUALS = [-0.25, -0.19, -0.13, -0.08, -0.04, 0.0, 0.04, 0.08, 0.13, 0.19, 0.25]


def calibrated(values):
    predicted = forecast(values)
    predicted.update(alarm_probability=0.2,
                     risk_quantiles={"space": "ln_residual", "probabilities": PROBABILITIES, "quantiles": RESIDUALS})
    return predicted


def test_forecast_alarm_leads_to_corrective_action_when_it_restores_the_margin(monkeypatch, tmp_path):
    predicted = calibrated((9.3, 9.4, 9.5, 9.5))
    predicted.update(alarm_above_10=True, exceedance_probability=0.38)
    decision = decide(monkeypatch, tmp_path, frame(), predicted=predicted)
    assert decision["status"] == "recommendation"
    hold = next(c for c in decision["candidates"] if c["id"] == "hold")
    assert hold["feasible"] is False and "Риск превышения" in hold["reason"]
    selected = decision["recommendation"]
    assert selected["controls"]["ht.T6"]["change"] > 0
    assert selected["risk"]["exceedance_probability"] <= 0.2
    codes = {c["code"]: c["resolution"] for c in decision["conflicts"]}
    assert codes["stability_vs_quality"] == "corrective_action"


def test_forecast_alarm_abstains_when_no_change_restores_the_margin(monkeypatch, tmp_path):
    predicted = calibrated((11.5, 11.8, 12.0, 12.2))
    predicted.update(alarm_above_10=True, exceedance_probability=0.95)
    decision = decide(monkeypatch, tmp_path, frame(), predicted=predicted)
    assert decision["status"] == "abstain"
    assert {c["code"]: c["resolution"] for c in decision["conflicts"]}["stability_vs_quality"] == "abstain"
    assert "Риск превышения" in decision["abstain"]["reason"] or "10 мг/кг" in decision["abstain"]["reason"]


def test_economic_move_may_not_spend_the_upper_half_of_the_risk_budget(monkeypatch, tmp_path):
    decision = agents_decide_with_economics(monkeypatch, tmp_path, calibrated((8.6, 8.6, 8.6, 8.6)))
    hold = next(c for c in decision["candidates"] if c["id"] == "hold")
    assert 0.1 < hold["risk"]["exceedance_probability"] < 0.2
    for candidate in decision["candidates"]:
        if candidate["id"] in {"lower_heat", "more_feed", "lower_pressure"}:
            assert not candidate["feasible"]
            assert "Экономия допустима только" in candidate["reason"]
    assert decision["selected_candidate"] == "hold"


def test_low_sulfur_margin_is_spent_on_the_cheapest_admissible_move(monkeypatch, tmp_path):
    decision = agents_decide_with_economics(monkeypatch, tmp_path, calibrated((6.0, 6.0, 6.0, 6.0)))
    assert decision["status"] == "recommendation"
    assert decision["selected_candidate"] != "hold"
    selected = decision["recommendation"]
    assert selected["risk"]["exceedance_probability"] <= 0.1
    assert selected["objectives"]["ranking_loss"] < next(
        c for c in decision["candidates"] if c["id"] == "hold")["objectives"]["ranking_loss"]


def agents_decide_with_economics(monkeypatch, tmp_path, predicted):
    monkeypatch.setattr(agents, "snapshot", lambda *args: frame())
    monkeypatch.setattr(agents, "forecast_sulfur", lambda *a, **k: deepcopy(predicted))
    monkeypatch.setattr(scenarios, "forecast_sulfur", lambda *a, **k: deepcopy(predicted))
    return agents.make_decision(tmp_path, ScenarioRequest(at=AT, optimize_economics=True))


def test_known_analyser_bias_is_not_a_data_conflict(monkeypatch, tmp_path):
    predicted = forecast()
    predicted["analysers"] = {"pak": {"adjusted": 8.3}, "q21": {"adjusted": 8.6}}
    decision = decide(monkeypatch, tmp_path, frame(ht__Q21={"value": 11.0}), predicted=predicted)
    comparison = decision["agents"]["quality"]["evidence"]["analyzer_comparison"]
    assert comparison["raw_difference_mgkg"] == 3.0
    assert comparison["difference_mgkg"] == pytest.approx(0.3)
    assert comparison["conflict"] is False
    assert decision["status"] == "recommendation"


def test_monthly_cetane_sample_is_used_with_drift_bound_and_improver(monkeypatch, tmp_path):
    snapshot = frame(lims__ht__2__CetaneNumber={"value": 51.5, "freshness": "stale",
                                                  "timestamp": "2026-04-11T10:00:00", "available_at": "2026-04-11T14:00:00"})
    decision = decide(monkeypatch, tmp_path, snapshot)
    assert decision["status"] == "recommendation"
    cetane = decision["agents"]["quality"]["evidence"]["other_quality"]["cetane"]
    assert cetane["age_days"] == pytest.approx(20.083, abs=0.01)
    assert cetane["lower_bound"] == pytest.approx(51.5 - 1.2816 * 0.311 * cetane["age_days"] ** 0.5)
    additive = decision["recommendation"]["additive"]
    assert additive["pct"] > 0 and cetane["lower_bound"] + 4 * additive["pct"] >= 51
    assert "свежий анализ" in " ".join(decision["agents"]["quality"]["warnings"])
