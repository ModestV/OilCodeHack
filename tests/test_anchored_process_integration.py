"""Forecast-to-inventory invariants; do not turn HT endpoint risk into batch quality."""
from pathlib import Path

import pytest

from backend import agents, scenarios
from backend.agents import AgentContext, OptimizationAgent
from backend.scenarios import ScenarioRequest, calculate_scenario, _batch_integral

AT = "2026-05-01T12:00:00"
FRAME = {"values": [{"metric_id": k, "value": v, "timestamp": AT, "available_at": AT,
                     "flags": [], "freshness": "fresh"} for k, v in
                    [("ht.T6", 360), ("ht.F9", 210), ("ht.P13", 4), ("ht.Q21", 8),
                     ("lims.ht.2.95%.T", 350), ("lims.ht.2.CetaneNumber", 52)]]}


def forecast(horizon=180, values=(8., 12., 6., 10.), upper=None):
    upper = upper or values
    position = int(horizon / 60)
    return {"status": "ok", "path_supported": True, "horizon_minutes": horizon,
            "prediction": values[position], "prediction_upper": upper[position],
            "nowcast": {"prediction": values[0]}, "alarm_above_10": values[position] > 10,
            "horizons": [{"minutes": h, "prediction": p, "upper": u, "status": "ok"}
                         for h, p, u in zip((0, 60, 120, 180), values, upper)]}


def request(**extra):
    params = dict(at=AT, batch_mass_t=50, production_rate_tph=40, changes={}, optimize_economics=False,
                  tanks=[dict(name="new", kind="hydrotreated_batch", stock_t=0, share=80,
                              sulfur=11, t95=350, cetane=52),
                         dict(name="stock", stock_t=100, share=20, sulfur=2, t95=300, cetane=53)])
    return ScenarioRequest(**(params | extra))


def test_forecast_integral_drives_arriving_batch_not_endpoint():
    req = request()
    result = calculate_scenario(Path('.'), req, frame=FRAME, forecast=forecast())
    assert result['baseline']['sulfur_source'] == 'model.nowcast'
    assert result['predicted_sulfur'] == 10
    assert result['batch']['produced_sulfur'] == pytest.approx(9)
    assert result['product_sulfur'] == pytest.approx(7.6)
    req.transport_delay_minutes = 60
    delayed = calculate_scenario(Path('.'), req, frame=FRAME, forecast=forecast())
    assert delayed['batch']['produced_sulfur'] == pytest.approx(9.5)
    assert delayed['batch']['produced_t'] == 80
    req.step_minutes = 15
    assert calculate_scenario(Path('.'), req, frame=FRAME, forecast=forecast())['batch'] == delayed['batch']


def test_manual_baseline_does_not_acquire_observed_forecast_dynamics():
    result = calculate_scenario(Path('.'), request(current_sulfur=11), frame=FRAME, forecast=forecast())
    assert result['predicted_sulfur'] == result['batch']['produced_sulfur'] == 11
    assert result['forecast'] is None


def test_guard_checks_early_peak_even_when_endpoint_is_safe():
    req = request()
    f = forecast(values=(8, 8, 8, 8), upper=(9, 15, 9, 9))
    result = calculate_scenario(Path('.'), req, frame=FRAME, forecast=f)
    gate = OptimizationAgent._gate(result, AgentContext(Path('.'), req, FRAME),
             {'can_recommend': True, 'basis': 'observed_and_forecast', 'reasons': []},
             {'evidence': {'model_forecast': f}})
    assert not gate['passed']
    assert any(c['name'] == 'blend_forecast_guard' and not c['passed'] for c in gate['checks'])


def test_guard_does_not_use_peak_that_has_not_arrived():
    req = request(transport_delay_minutes=120, batch_mass_t=20)
    f = forecast(values=(8, 8, 8, 8), upper=(9, 9, 15, 15))
    result = calculate_scenario(Path('.'), req, frame=FRAME, forecast=f)
    gate = OptimizationAgent._gate(result, AgentContext(Path('.'), req, FRAME),
             {'can_recommend': True, 'basis': 'observed_and_forecast', 'reasons': []},
             {'evidence': {'model_forecast': f}})
    assert gate['passed']


def test_h1_decision_requests_h1_and_replays_same_model_and_mass(monkeypatch, tmp_path):
    calls = []
    def predict(directory, at, *, horizon_minutes):
        calls.append(horizon_minutes)
        return forecast(horizon_minutes, values=(8, 8, 8, 8))
    monkeypatch.setattr(agents, 'snapshot', lambda *args: FRAME)
    monkeypatch.setattr(agents, 'forecast_sulfur', predict)
    monkeypatch.setattr(scenarios, 'forecast_sulfur', predict)
    req = request(horizon_minutes=60, batch_mass_t=20)
    decision = agents.make_decision(tmp_path, req)
    assert calls == [60]  # a single forecast is shared across every candidate
    assert decision['status'] == 'recommendation'
    replay = calculate_scenario(tmp_path, ScenarioRequest(**decision['scenario']['model_request']), frame=FRAME)
    assert replay['batch'] == decision['scenario']['batch']
    assert replay['blend'] == decision['scenario']['blend']


def test_partial_forecast_path_abstains_instead_of_using_flat_baseline(monkeypatch, tmp_path):
    f = forecast()
    f['path_supported'] = False
    monkeypatch.setattr(agents, 'snapshot', lambda *args: FRAME)
    monkeypatch.setattr(agents, 'forecast_sulfur', lambda *args, **kwargs: f)
    result = agents.make_decision(tmp_path, request())
    assert result['status'] == 'abstain'
    assert any('горизонты' in r for r in result['agents']['reliability']['reasons'])


def test_piecewise_integral_handles_zero_crossing_and_instantaneous_step():
    assert _batch_integral([(0, 10), (60, 10)], -20, 60, 60, 0) == pytest.approx(150)
    assert _batch_integral([(0, 10), (60, 10)], -4, 60, 0, 30) == pytest.approx(480)
