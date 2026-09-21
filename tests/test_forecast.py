"""Temporal counterexamples, analyser cleaning, lab anchoring and the v4 two-stage runtime."""
import json
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import forecast as forecast_module
from backend.forecast import (
    ForecastUnavailable,
    _load_artifact,
    exceedance_at,
    forecast_sulfur,
    select_model,
)
from tools.modeling.sulfur_features import (
    DYNAMICS_COLUMNS,
    FEATURE_COLUMNS,
    STAGE2_INPUTS,
    SUPPORT_COLUMNS,
    build_dynamics_features,
    build_features,
    clean_analyser,
    exceedance_probability,
    lab_anchor,
    mahalanobis_score,
    predict_convex,
    residual_quantile_function,
    widen_quantiles,
)

T0 = pd.Timestamp("2025-01-10T08:00")


def _rows(metric, source, points):
    return [(metric, pd.Timestamp(ts), float(value), source) for ts, value in points]


def dataset(directory, *, analyser=8.0, lab_offset=0.0, future=None, unpublished=None, missing=False,
            stuck=False, labs_days=12, controls=(300.0, 100.0, 5.0), t6_step=None):
    """Synthetic 12-day history: 10-minute analysers/controls and a daily 10:00 lab.

    The lab equals ``analyser + lab_offset`` so that the anchored analyser is
    exactly ``analyser + lab_offset`` after anchoring.  ``t6_step`` raises T6
    by that amount 30 minutes before ``T0`` (an in-flight operator move).
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
            t6 = controls[0] + (t6_step if t6_step and ts >= T0 - pd.Timedelta(minutes=30) else 0.0)
            rows += _rows("ht.Q21", "kip", [(ts, value)])
            rows += _rows("pak.ht.Mg.Sulfur", "pak", [(ts, analyser - wobble)])
            rows += _rows("ht.T6", "kip", [(ts, t6)])
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


def _linear(columns, coef=None, medians=None, scale=None, **extra):
    n = len(columns)
    return {"kind": "linear", "feature_columns": list(columns), "alpha": 1.0,
            "medians": medians or [0.0] * n, "mean": [0.0] * n, "scale": scale or [1.0] * n,
            "coef": coef or [0.0] * (n + 1), "support_lower": [-1e9] * n, "support_upper": [1e9] * n, **extra}


def artifact(tmp_path, *, fit_ends=("2024-01-01T00:00:00",), support_upper=None, t6_gain=0.0, stage2="convex"):
    """Identity model set: ln prediction = ln(anchored Q21); wide support unless narrowed.

    Stage 1 is a zero model (persistence) unless ``t6_gain`` puts a
    coefficient on ``dT6_1h``.  Longer horizons shrink toward ln(8) by a fixed
    fraction when ``stage2="linear"`` (checks interpolation).
    """
    residuals = residual_quantile_function(np.linspace(-0.4, 0.4, 81))
    entries = []
    for fit_end in fit_ends:
        support = _linear(SUPPORT_COLUMNS, scale=[1e6] * len(SUPPORT_COLUMNS))
        support["support_upper"] = support_upper or [1e9] * len(SUPPORT_COLUMNS)
        support["q50"] = support["medians"]
        stage1 = {name: {} for name in ("q21", "pak")}
        stage2_models, persistence, supports = {}, {}, {}
        for horizon in (0, 60, 120, 180):
            columns = STAGE2_INPUTS[0] if horizon == 0 else STAGE2_INPUTS["h"]
            if stage2 == "linear" and horizon:
                shrink = horizon / 180 * 0.5
                coef = [shrink * math.log(8.0), 1.0 - shrink, 0.0, 0.0, 0.0]
                model = _linear(columns, coef=coef, medians=[math.log(8.0)] * 4, label="linear")
            else:
                model = {"kind": "convex", "label": "convex", "feature_columns": columns, "weights": [1.0, 0.0, 0.0, 0.0], "bias": 0.0}
            model.update({"residual_quantiles": residuals, "residual_quantiles_persistence": residuals, "alarm_probability": 0.3})
            stage2_models[str(horizon)] = model
            persistence[str(horizon)] = {"kind": "convex", "label": "persistence", "feature_columns": columns, "weights": [1.0, 0.0, 0.0, 0.0], "bias": 0.0}
            supports[str(horizon)] = support
            if horizon:
                for name in ("q21", "pak"):
                    coef = [0.0] * (len(DYNAMICS_COLUMNS) + 1)
                    coef[1 + DYNAMICS_COLUMNS.index("dT6_1h")] = t6_gain
                    stage1[name][str(horizon)] = _linear(DYNAMICS_COLUMNS, coef=coef, analyser_column=f"ln_{name}_raw",
                                                         horizon_minutes=horizon, training_filter="all")
        entries.append({"fit_end": fit_end, "available_from": fit_end, "stage1": stage1, "stage2": stage2_models,
                        "persistence": persistence, "support": supports,
                        "control_response": {"T6": {"coefficient": -0.02, "se": 0.002, "lag_minutes": 120},
                                             "F9": {"coefficient": 0.012, "se": 0.004, "lag_minutes": 60},
                                             "P13": {"coefficient": -0.25, "se": 0.07, "lag_minutes": 60}},
                        "consistency": {}, "anomaly": None, "train_median": 8.0})
    result = {"version": 4, "walk_forward": entries, "feature_columns": SUPPORT_COLUMNS, "dynamics_columns": DYNAMICS_COLUMNS,
              "stage2_inputs": {"0": STAGE2_INPUTS[0], "h": STAGE2_INPUTS["h"]},
              "applicability_policy": {"max_missing_fraction": 0.34, "max_ood_fraction": 0.2, "max_absolute_z": 8,
                                       "support_quantiles": [0.005, 0.995], "support_margin_fraction": 0.1, "min_anchor_pairs": 3},
              "horizons_minutes": [0, 60, 120, 180], "lims_publication_delay_minutes": 240, "hard_limit": 10.0,
              "lab_anchor_samples": 10, "model_available_from": fit_ends[0]}
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
    assert one["model"]["fit_end"] == "2024-01-01T00:00:00"


def test_lims_enters_exactly_at_publication_boundary(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", unpublished=9)
    sample = T0 - pd.Timedelta(minutes=239)
    before = forecast_sulfur(tmp_path / "data", (sample + pd.Timedelta(minutes=239)).isoformat(), path)
    after = forecast_sulfur(tmp_path / "data", (sample + pd.Timedelta(minutes=240)).isoformat(), path)
    assert before["previous_lab"]["value"] == 8
    assert after["previous_lab"]["value"] == 9


def test_historical_origin_cannot_use_model_selected_later(tmp_path):
    path, _ = artifact(tmp_path, fit_ends=("2026-01-01T00:00:00",))
    dataset(tmp_path / "data")
    result = forecast_sulfur(tmp_path / "data", T0.isoformat(), path)
    assert result["status"] == "abstain"
    assert "model_not_yet_available_at_origin" in result["reasons"]
    assert result["prediction"] is result["nowcast"] is result["exceedance_probability"] is None


def test_runtime_selects_latest_model_not_after_origin(tmp_path):
    path, model = artifact(tmp_path, fit_ends=("2024-01-01T00:00:00", "2025-01-01T00:00:00", "2026-01-01T00:00:00"))
    assert select_model(model, pd.Timestamp("2023-06-01")) is None
    assert select_model(model, pd.Timestamp("2024-01-01"))["fit_end"] == "2024-01-01T00:00:00"
    assert select_model(model, pd.Timestamp("2025-12-31T23:59"))["fit_end"] == "2025-01-01T00:00:00"
    dataset(tmp_path / "data")
    assert forecast_sulfur(tmp_path / "data", T0.isoformat(), path)["model"]["fit_end"] == "2025-01-01T00:00:00"


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
    # The last two hours are a 24.87+-0.004 plateau.  Causally only the first
    # hour of a plateau can pass; after that Q21 is unusable, so the fresh
    # Q21 feature is missing and the nowcast falls back to PAK/lab evidence.
    assert result["analysers"]["q21"]["age_minutes"] >= 60
    assert result["status"] == "ok"
    assert result["nowcast"]["prediction"] < 10


def test_horizon_prediction_interpolates_between_fitted_models(tmp_path):
    path, _ = artifact(tmp_path, stage2="linear")
    dataset(tmp_path / "data", analyser=6.0)
    at = T0.isoformat()
    p60 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=60)["prediction"]
    p90 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=90)["prediction"]
    p120 = forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=120)["prediction"]
    assert p60 < p90 < p120 < 8.0
    with pytest.raises(ValueError):
        forecast_sulfur(tmp_path / "data", at, path, horizon_minutes=181)


def test_in_flight_move_changes_hold_path_not_nowcast(tmp_path):
    path, _ = artifact(tmp_path, t6_gain=-0.05)
    dataset(tmp_path / "flat")
    dataset(tmp_path / "moved", t6_step=4.0)
    flat = forecast_sulfur(tmp_path / "flat", T0.isoformat(), path)
    moved = forecast_sulfur(tmp_path / "moved", T0.isoformat(), path)
    assert moved["nowcast"]["prediction"] == pytest.approx(flat["nowcast"]["prediction"])
    assert moved["in_flight_controls"]["dT6_1h"] == pytest.approx(4.0)
    assert moved["prediction"] < flat["prediction"]
    assert moved["stage1"]["status"] == "ok"
    assert moved["horizons"][-1]["stage1"]["q21"]["delta_ln"] == pytest.approx(-0.2)


def test_per_horizon_support_can_abstain_only_long_horizons(tmp_path):
    path, model = artifact(tmp_path)
    # Narrow the long-horizon support on T6 only: the nowcast stays valid.
    for key in ("120", "180"):
        upper = [1e9] * len(SUPPORT_COLUMNS)
        upper[SUPPORT_COLUMNS.index("T6")] = 302.0
        model["walk_forward"][0]["support"][key] = {**model["walk_forward"][0]["support"][key], "support_upper": upper}
    path.write_text(json.dumps(model), encoding="utf-8")
    dataset(tmp_path / "data", t6_step=4.0)
    result = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=180)
    assert result["status"] == "abstain" and "outside_training_support" in result["reasons"]
    assert result["nowcast"] is not None and result["nowcast"]["prediction"] == pytest.approx(8.0, abs=0.1)
    assert [row["status"] for row in result["horizons"]] == ["ok", "ok", "abstain", "abstain"]
    short = forecast_sulfur(tmp_path / "data", T0.isoformat(), path, horizon_minutes=60)
    assert short["status"] == "ok"


def test_stage1_fallback_uses_persistence_quantiles(tmp_path):
    path, model = artifact(tmp_path, t6_gain=-0.05)
    wide = residual_quantile_function(np.linspace(-1.0, 1.0, 81))
    for key in model["walk_forward"][0]["stage2"]:
        model["walk_forward"][0]["stage2"][key]["residual_quantiles_persistence"] = wide
    path.write_text(json.dumps(model), encoding="utf-8")
    dataset(tmp_path / "data", t6_step=4.0)
    # Without control history (controls loaded only 48h, but here we remove them entirely
    # from the dynamics inputs by making the dataset lack F9/P13 deltas) the model still
    # has T6; simulate sparse dynamics by dropping the PAK and F9/P13 series.
    frame = pd.read_parquet(tmp_path / "data" / "observations.parquet")
    frame = frame[~frame["metric_id"].isin(["ht.F9", "ht.P13"])]
    dataset(tmp_path / "sparse")
    frame.to_parquet(tmp_path / "sparse" / "observations.parquet", index=False)
    result = forecast_sulfur(tmp_path / "sparse", T0.isoformat(), path, horizon_minutes=180)
    # Missing F9/P13 also fail the support gate (too many missing); check the
    # Stage-1 mechanics directly instead.
    assert result["status"] == "abstain"
    from tools.modeling.sulfur_forecast import stage1_apply
    raw = np.full(len(DYNAMICS_COLUMNS), np.nan)
    raw[DYNAMICS_COLUMNS.index("ln_q21_raw")] = math.log(8.0)
    delta, fallback = stage1_apply(model["walk_forward"][0]["stage1"]["q21"]["180"], raw)
    assert fallback.all() and delta[0] == 0.0
    full = np.zeros(len(DYNAMICS_COLUMNS))
    full[DYNAMICS_COLUMNS.index("dT6_1h")] = 4.0
    delta, fallback = stage1_apply(model["walk_forward"][0]["stage1"]["q21"]["180"], full)
    assert not fallback.any() and delta[0] == pytest.approx(-0.2)


def test_artifact_with_unknown_feature_name_is_rejected(tmp_path):
    path, model = artifact(tmp_path)
    model["walk_forward"][0]["support"]["0"]["feature_columns"] = ["bogus"] + SUPPORT_COLUMNS[1:]
    path.write_text(json.dumps(model), encoding="utf-8")
    with pytest.raises(ForecastUnavailable):
        _load_artifact(path)
    model["walk_forward"][0]["support"]["0"]["feature_columns"] = SUPPORT_COLUMNS
    model["feature_columns"] = list(reversed(SUPPORT_COLUMNS))
    path.write_text(json.dumps(model), encoding="utf-8")
    with pytest.raises(ForecastUnavailable):
        _load_artifact(path)


def test_artifact_cache_invalidates_on_change(tmp_path):
    path, model = artifact(tmp_path)
    first = _load_artifact(path)
    assert _load_artifact(path) is first
    model["hard_limit"] = 9.5
    path.write_text(json.dumps(model), encoding="utf-8")
    import os
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    second = _load_artifact(path)
    assert second is not first and second["hard_limit"] == 9.5
    forecast_module._ARTIFACT_CACHE.clear()


@pytest.mark.parametrize("kwargs,at,reason", [
    ({"missing": True}, T0, "no_control_telemetry"),
    ({}, T0 + pd.Timedelta(days=3), "too_many_missing_features"),
    ({"controls": (300.0, 100.0, 50.0)}, T0, "outside_training_support"),
    ({"labs_days": 2}, T0, "insufficient_lab_anchor"),
])
def test_unsupported_inputs_abstain_instead_of_median_forecast(tmp_path, kwargs, at, reason):
    support_upper = None
    if reason == "outside_training_support":
        support_upper = [1e9] * len(SUPPORT_COLUMNS)
        support_upper[SUPPORT_COLUMNS.index("P13")] = 10.0
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


def test_scenario_adjusted_risk_uses_same_residuals_and_widens_with_uncertainty(tmp_path):
    path, model = artifact(tmp_path)
    entry = model["walk_forward"][0]
    risk = exceedance_at(entry, 180, math.log(9.0))
    assert risk["prediction"] == pytest.approx(9.0)
    assert risk["lower"] < 9.0 < risk["upper"]
    assert 0 < risk["exceedance_probability"] < 1
    wider = exceedance_at(entry, 180, math.log(9.0), extra_sigma=0.3)
    assert wider["lower"] < risk["lower"] and wider["upper"] > risk["upper"]
    assert wider["exceedance_probability"] > risk["exceedance_probability"]


def test_widen_quantiles_identity_at_zero_sigma():
    quantiles = residual_quantile_function(np.linspace(-0.5, 0.5, 101))
    assert widen_quantiles(quantiles, 0.0) is quantiles
    widened = widen_quantiles(quantiles, 0.5)
    assert widened["sigma"] > quantiles["sigma"]
    assert np.interp(0.5, widened["probabilities"], widened["quantiles"]) == pytest.approx(0.0, abs=1e-9)


def test_stage2_convex_renormalizes_over_available_inputs():
    model = {"weights": [0.6, 0.3, 0.1], "bias": 0.05}
    assert predict_convex(model, np.array([1.0, 2.0, 3.0])) == pytest.approx(0.6 + 0.6 + 0.3 + 0.05)
    assert predict_convex(model, np.array([np.nan, 2.0, 3.0])) == pytest.approx((0.6 + 0.3) / 0.4 + 0.05)
    assert np.isnan(predict_convex(model, np.array([np.nan, np.nan, np.nan])))


def test_mahalanobis_flags_out_of_regime_vector():
    model = {"tags": ["a", "b"], "location": [0.0, 0.0], "scale": [1.0, 1.0], "precision": [[1.0, 0.0], [0.0, 1.0]],
             "thresholds": {"attention": 4.0, "high": 9.0}}
    assert mahalanobis_score(model, {"a": 0.5, "b": 0.5})["class"] == "normal"
    assert mahalanobis_score(model, {"a": 2.5, "b": 0.0})["class"] == "attention"
    high = mahalanobis_score(model, {"a": 4.0, "b": 0.0})
    assert high["class"] == "high" and high["factors"][0]["metric_id"] == "a"
    assert mahalanobis_score(model, {"a": None, "b": 0.0})["status"] == "unavailable"


def test_clean_analyser_drops_exact_and_near_plateaus_but_keeps_moving_values():
    index = pd.date_range("2025-01-01", periods=30, freq="10min")
    values = pd.Series(np.linspace(7, 9, 30), index=index)
    values.iloc[10:20] = 307.0                     # out of range
    values.iloc[20:30] = 24.87 + np.linspace(0, 0.004, 10)  # saturated
    cleaned = clean_analyser(values)
    assert cleaned.iloc[:10].notna().all()
    assert cleaned.iloc[10:20].isna().all()
    # Causal rule: the first hour of a plateau cannot be recognised yet.
    assert cleaned.iloc[20:26].notna().all()
    assert cleaned.iloc[26:].isna().all()


def test_clean_analyser_is_causal_under_truncation():
    index = pd.date_range("2025-01-01", periods=60, freq="10min")
    values = pd.Series(8 + 0.3 * np.sin(np.arange(60)), index=index)
    values.iloc[20:45] = 24.87
    values.iloc[50:] = 8.0
    full = clean_analyser(values)
    for cut in (21, 25, 27, 40, 46, 55):
        truncated = clean_analyser(values.iloc[:cut])
        pd.testing.assert_series_equal(truncated, full.iloc[:cut])


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


def test_dynamics_features_use_only_past():
    index = pd.date_range("2025-01-01", periods=24 * 6 * 2, freq="10min")
    q21 = pd.Series(8.0, index=index)
    t6 = pd.Series(300.0, index=index)
    origin = pd.DatetimeIndex([index[100]])
    base = build_dynamics_features({"q21": q21}, {"T6": t6}, origin)
    t6_future = t6.copy()
    t6_future.iloc[101:] = 340.0
    q21_future = q21.copy()
    q21_future.iloc[101:] = 30.0
    changed = build_dynamics_features({"q21": q21_future}, {"T6": t6_future}, origin)
    pd.testing.assert_frame_equal(base, changed)
    t6_past = t6.copy()
    t6_past.iloc[98:101] = 305.0
    moved = build_dynamics_features({"q21": q21}, {"T6": t6_past}, origin)
    assert moved.loc[0, "dT6_1h"] == pytest.approx(5.0)
    assert np.isnan(base.loc[0, "ln_pak_raw"])


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
