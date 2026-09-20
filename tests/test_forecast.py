"""Temporal counterexamples, analyser cleaning and lab anchoring for the v3 forecast."""
import json
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.forecast import exceedance_at, forecast_sulfur
from tools.modeling.sulfur_features import (FEATURE_COLUMNS, MODEL_COLUMNS, build_features, clean_analyser,
                                            exceedance_probability, lab_anchor, residual_quantile_function)

T0 = pd.Timestamp("2025-01-10T08:00")


def _rows(metric, source, points):
    return [(metric, pd.Timestamp(ts), float(value), source) for ts, value in points]


def dataset(directory, *, analyser=8.0, lab_offset=0.0, future=None, unpublished=None, missing=False,
            stuck=False, labs_days=12, controls=(300.0, 100.0, 5.0)):
    """Synthetic 12-day history: 10-minute analysers/controls and a daily 10:00 lab.

    The lab equals ``analyser + lab_offset`` so that the anchored analyser is
    exactly ``analyser + lab_offset`` after anchoring.
    """
    directory.mkdir(parents=True)
    rows = []
    if not missing:
        grid = pd.date_range(T0 - pd.Timedelta(days=labs_days), T0, freq="10min")
        for i, ts in enumerate(grid):
            # A live analyser wobbles; a constant series would be a plateau.
            wobble = 0.05 * math.sin(i)
            value = analyser + wobble
            if stuck and ts >= T0 - pd.Timedelta(hours=2):
                value = 24.87 + 0.004 * math.sin(i)  # saturated analyser, +-0.004 noise
            rows += _rows("ht.Q21", "kip", [(ts, value)])
            rows += _rows("pak.ht.Mg.Sulfur", "pak", [(ts, analyser - wobble)])
            rows += _rows("ht.T6", "kip", [(ts, controls[0])])
            rows += _rows("ht.F9", "kip", [(ts, controls[1])])
            rows += _rows("ht.P13", "kip", [(ts, controls[2])])
        if future is not None:
            rows += _rows("ht.Q21", "kip", [(T0 + pd.Timedelta(minutes=10), future)])
    for day in range(labs_days, 0, -1):
        rows += _rows("lims.ht.2.Mg.Sulfur", "lims", [(T0 - pd.Timedelta(days=day) + pd.Timedelta(hours=2), analyser + lab_offset)])
    if unpublished is not None:
        rows += _rows("lims.ht.2.Mg.Sulfur", "lims", [(T0 - pd.Timedelta(minutes=239), unpublished)])
    frame = pd.DataFrame(rows, columns=["metric_id", "timestamp", "value", "source"])
    frame["flags"] = ""
    frame.to_parquet(directory / "observations.parquet", index=False)
    (directory / "manifest.json").write_text('{"id":"dataset","status":"ready"}')


def artifact(tmp_path, *, available_from=None, support_upper=None):
    """Identity model: ln prediction = ln(anchored Q21); wide support unless narrowed."""
    n = len(MODEL_COLUMNS)
    residuals = residual_quantile_function(np.linspace(-0.4, 0.4, 81))
    support_columns = FEATURE_COLUMNS
    support = {"feature_columns": support_columns, "medians": [0.0] * len(support_columns),
               "mean": [0.0] * len(support_columns), "scale": [1e6] * len(support_columns),
               "coef": [0.0] * (len(support_columns) + 1),
               "support_lower": [-1e9] * len(support_columns),
               "support_upper": support_upper or [1e9] * len(support_columns)}
    models = {}
    for horizon in (0, 60, 120, 180):
        coef = [0.0] * (n + 1)
        coef[1 + MODEL_COLUMNS.index("ln_q21")] = 1.0
        # Longer horizons shrink toward ln(8) by a fixed fraction (checks interpolation).
        shrink = horizon / 180 * 0.5
        coef[1 + MODEL_COLUMNS.index("ln_q21")] = 1.0 - shrink
        coef[0] = shrink * math.log(8.0)
        models[str(horizon)] = {"feature_columns": MODEL_COLUMNS, "alpha": 1.0, "medians": [math.log(8.0)] * n,
                                "mean": [0.0] * n, "scale": [1.0] * n, "coef": coef,
                                "support_lower": [-1e9] * n, "support_upper": [1e9] * n, "support": support,
                                "residual_quantiles": residuals, "alarm_probability": 0.3}
    result = {"version": 3, "models": models, "model_columns": MODEL_COLUMNS, "feature_columns": FEATURE_COLUMNS,
              "applicability_policy": {"max_missing_fraction": 0.34, "max_ood_fraction": 0.2, "max_absolute_z": 8,
                                       "support_quantiles": [0.005, 0.995], "support_margin_fraction": 0.1, "min_anchor_pairs": 3},
              "horizons_minutes": [0, 60, 120, 180], "lims_publication_delay_minutes": 240, "hard_limit": 10.0,
              "lab_anchor_samples": 10, "control_response": {}, "model_available_from": available_from}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path, result


def test_future_telemetry_and_unpublished_lims_cannot_change_forecast(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "a", future=900, unpublished=500)
    dataset(tmp_path / "b", future=1, unpublished=1)
    one = forecast_sulfur(tmp_path / "a", T0.isoformat(), path)
    two = forecast_sulfur(tmp_path / "b", T0.isoformat(), path)
    assert one["status"] == two["status"] == "ok"
    assert one["prediction"] == two["prediction"]
    assert one["nowcast"]["prediction"] == pytest.approx(8.0, abs=0.1)
    assert one["previous_lab"]["value"] == 8
    assert one["feature_time"] == T0.isoformat()
    assert one["target_time"] == (T0 + pd.Timedelta(minutes=180)).isoformat()
    assert one["leakage_check"]["passed"]


def test_lims_enters_exactly_at_publication_boundary(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", unpublished=9)
    sample = T0 - pd.Timedelta(minutes=239)
    before = forecast_sulfur(tmp_path / "data", (sample + pd.Timedelta(minutes=239)).isoformat(), path)
    after = forecast_sulfur(tmp_path / "data", (sample + pd.Timedelta(minutes=240)).isoformat(), path)
    assert before["previous_lab"]["value"] == 8
    assert after["previous_lab"]["value"] == 9


def test_historical_origin_cannot_use_model_selected_later(tmp_path):
    path, _ = artifact(tmp_path, available_from="2026-01-01T00:00:00")
    dataset(tmp_path / "data")
    result = forecast_sulfur(tmp_path / "data", T0.isoformat(), path)
    assert result["status"] == "abstain"
    assert "model_not_yet_available_at_origin" in result["reasons"]
    assert result["prediction"] is result["nowcast"] is result["exceedance_probability"] is None


def test_lab_anchoring_corrects_analyser_offset(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", analyser=7.0, lab_offset=1.5)
    result = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=0)
    assert result["analysers"]["q21"]["value"] == pytest.approx(7.0, abs=0.06)
    assert result["analysers"]["q21"]["offset"] == pytest.approx(1.5, abs=0.06)
    assert result["analysers"]["q21"]["adjusted"] == pytest.approx(8.5, abs=0.1)
    assert result["nowcast"]["prediction"] == pytest.approx(8.5, abs=0.1)
    assert result["lab_anchor"]["pairs"] == 10


def test_saturated_analyser_plateau_is_not_evidence(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", stuck=True)
    result = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=0)
    # The last two hours are a 24.87+-0.004 plateau: unusable, so the fresh
    # Q21 feature is missing and the nowcast falls back to PAK/lab evidence.
    assert result["analysers"]["q21"]["age_minutes"] >= 60
    assert result["status"] == "ok"
    assert result["nowcast"]["prediction"] < 10


def test_horizon_prediction_interpolates_between_fitted_models(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", analyser=6.0)
    at = T0.isoformat()
    p60 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=60)["prediction"]
    p90 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=90)["prediction"]
    p120 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=120)["prediction"]
    assert p60 < p90 < p120 < 8.0
    with pytest.raises(ValueError):
        forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=181)


@pytest.mark.parametrize("kwargs,at,reason", [
    ({"missing": True}, T0, "no_control_telemetry"),
    ({}, T0 + pd.Timedelta(days=3), "too_many_missing_features"),
    ({"controls": (300.0, 100.0, 50.0)}, T0, "outside_training_support"),
    ({"labs_days": 2}, T0, "insufficient_lab_anchor"),
])
def test_unsupported_inputs_abstain_instead_of_median_forecast(tmp_path, kwargs, at, reason):
    support_upper = None
    if reason == "outside_training_support":
        support_upper = [1e9] * len(FEATURE_COLUMNS)
        support_upper[FEATURE_COLUMNS.index("P13")] = 10.0
    path, _ = artifact(tmp_path, support_upper=support_upper)
    dataset(tmp_path / "data", **kwargs)
    result = forecast_sulfur(tmp_path / "data", pd.Timestamp(at).isoformat(), path)
    assert result["status"] == "abstain"
    assert reason in result["reasons"]
    assert result["prediction"] is result["exceedance_probability"] is result["alarm_above_10"] is None


def test_exceedance_probability_follows_residual_distribution():
    quantiles = residual_quantile_function(np.linspace(-0.5, 0.5, 101))
    assert exceedance_probability(quantiles, math.log(10.0), math.log(10.0)) == pytest.approx(0.5, abs=0.02)
    assert exceedance_probability(quantiles, math.log(5.0), math.log(10.0)) == pytest.approx(0.01, abs=0.01)
    assert exceedance_probability(quantiles, math.log(20.0), math.log(10.0)) == pytest.approx(0.99, abs=0.01)
    low = exceedance_probability(quantiles, math.log(8.0), math.log(10.0))
    high = exceedance_probability(quantiles, math.log(9.5), math.log(10.0))
    assert low < high


def test_scenario_adjusted_risk_uses_same_residuals(tmp_path):
    path, model = artifact(tmp_path)
    risk = exceedance_at({**model, "sha256": "x"}, 180, math.log(9.0))
    assert risk["prediction"] == pytest.approx(9.0)
    assert risk["lower"] < 9.0 < risk["upper"]
    assert 0 < risk["exceedance_probability"] < 1


def test_clean_analyser_drops_exact_and_near_plateaus_but_keeps_moving_values():
    index = pd.date_range("2025-01-01", periods=30, freq="10min")
    values = pd.Series(np.linspace(7, 9, 30), index=index)
    values.iloc[10:20] = 307.0                     # out of range
    values.iloc[20:30] = 24.87 + np.linspace(0, 0.004, 10)  # saturated
    cleaned = clean_analyser(values)
    assert cleaned.iloc[:10].notna().all()
    assert cleaned.iloc[10:20].isna().all()
    assert cleaned.iloc[20:26].notna().all()  # plateau not yet established
    assert cleaned.iloc[26:].isna().all()


def test_plateau_cleaning_is_prefix_invariant():
    times = pd.date_range("2026-01-01", periods=8, freq="10min")
    series = pd.Series([7., 8., 8., 8., 8., 8., 8., 8.], index=times)
    full = clean_analyser(series)
    for end in times:
        pd.testing.assert_series_equal(clean_analyser(series.loc[:end]), full.loc[:end])


def test_each_horizon_and_nowcast_has_its_own_support(tmp_path):
    path, model = artifact(tmp_path)
    dataset(tmp_path / "data")
    model["models"]["0"]["support"] = json.loads(json.dumps(model["models"]["0"]["support"]))
    model["models"]["0"]["support"]["support_upper"][FEATURE_COLUMNS.index("T6")] = 250
    path.write_text(json.dumps(model), encoding="utf8")
    now = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=0)
    future = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=180)
    between = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=30)
    assert now["status"] == between["status"] == "abstain"
    assert future["status"] == "ok" and future["prediction"] is not None
    assert future["nowcast"] is None and future["path_supported"] is False
    assert future["horizons"][0]["prediction"] is None


def test_last_lab_age_is_measured_from_sampling():
    from tools.modeling.sulfur_features import available_lab
    labs = pd.DataFrame({"target_time": [T0], "target": [8.]})
    result = available_lab(labs, pd.DatetimeIndex([T0 + pd.Timedelta(hours=48), T0 + pd.Timedelta(hours=48, minutes=1)]))
    assert result.iloc[0].previous_lab_available == 8
    assert pd.isna(result.iloc[1].previous_lab_available)


def test_portable_raw_fusion_respects_convex_hull_and_floors_negative_regression():
    from tools.modeling.sulfur_features import predict_portable
    model = {"feature_columns": ["ln_q21", "ln_level"], "medians": [6., 8.],
             "mean": [0., 0.], "scale": [1., 1.], "coef": [0., .25, .75],
             "input_transform": "exp", "output_transform": "log"}
    assert math.exp(predict_portable(model, np.log([4., 8.]))) == pytest.approx(7.)
    assert math.exp(predict_portable(model, np.array([np.nan, math.log(8.)]))) == pytest.approx(7.5)
    model["coef"] = [-20., .25, .75]
    assert math.exp(predict_portable(model, np.log([4., 8.]))) == pytest.approx(.3)


def test_lab_anchor_uses_only_published_samples():
    index = pd.date_range("2025-01-01", periods=24 * 6 * 5, freq="10min")
    analyser = pd.Series(7.0, index=index)
    labs = pd.DataFrame({"target_time": pd.to_datetime(["2025-01-01T10:00", "2025-01-02T10:00", "2025-01-03T10:00"]),
                         "target": [8.0, 8.0, 12.0]})
    origins = pd.DatetimeIndex(["2025-01-03T13:59", "2025-01-03T14:00"])
    anchor = lab_anchor(labs, {"q21": analyser}, origins)
    assert anchor.loc[0, "anchor_pairs_q21"] == 2 and anchor.loc[0, "offset_q21"] == pytest.approx(1.0)
    assert anchor.loc[1, "anchor_pairs_q21"] == 3 and anchor.loc[1, "offset_q21"] == pytest.approx(1.0)
    assert anchor.loc[1, "level"] == pytest.approx(8.0)


def test_build_features_shape_is_stable_without_sources():
    frame = build_features({}, {}, pd.DataFrame({"target_time": pd.to_datetime([]), "target": []}),
                           pd.DatetimeIndex(["2025-01-01"]))
    assert list(frame.columns[:len(FEATURE_COLUMNS)]) == FEATURE_COLUMNS
    assert frame[FEATURE_COLUMNS].isna().all().all()


def test_forecast_endpoint_reports_missing_coverage(tmp_path, monkeypatch):
    dataset(tmp_path / "storage" / "dataset", missing=True)
    monkeypatch.setattr(app_module, "STORAGE", tmp_path / "storage")
    with TestClient(app_module.app) as client:
        response = client.get("/api/datasets/dataset/forecast", params={"at": T0.isoformat(), "horizon_minutes": 60})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "abstain"
    assert body["horizon_minutes"] == 60
    assert body["target"]["metric_id"] == "lims.ht.2.Mg.Sulfur"
    assert body["leakage_check"]["passed"]
    assert body["prediction"] is None
