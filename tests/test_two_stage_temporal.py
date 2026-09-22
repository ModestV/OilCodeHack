"""Temporal-validity regressions for the repaired two-stage (v4r) sulphur forecast.

Each test pins one defect found in the audit of ``feat/forecast-v4-contour-v2``
(see reports/review/v4-repair-2026-09-22/PROTOCOL.md): target windows that
cross a fold end, a quiet-future filter that only compared endpoints,
same-fold calibration selection, full-period support gates, and future
mutations of features.
"""
import json

import numpy as np
import pandas as pd
import pytest

from tools.modeling import two_stage_forecast as tsf
from tools.modeling.two_stage_features import (
    build_dynamics_features,
    build_features,
    clean_analyser,
    future_path_quiet,
)

GRID = pd.date_range("2025-01-01", periods=3 * 24 * 6, freq="10min")
ORIGIN = pd.Timestamp("2025-01-02T12:00")


def _controls(t6=None, f9=None, p13=None, index=GRID):
    return {"T6": t6 if t6 is not None else pd.Series(350.0, index=index),
            "F9": f9 if f9 is not None else pd.Series(180.0, index=index),
            "P13": p13 if p13 is not None else pd.Series(5.0, index=index)}


def _dynamics(**controls):
    analyser = pd.Series(8 + 0.1 * np.sin(np.arange(len(GRID))), index=GRID)
    return tsf.DynamicsData({"q21": analyser}, _controls(**controls))


def _quiet(data, horizon=60, origin=ORIGIN):
    return bool(data.quiet[horizon][data.origins.get_loc(origin)])


# ---------------------------------------------------------------------------
# 1. Target window vs fold end
# ---------------------------------------------------------------------------

def test_target_window_crossing_fold_end_is_not_a_fold_row():
    data = _dynamics()
    end = ORIGIN + pd.Timedelta(minutes=60)
    start = ORIGIN - pd.Timedelta(hours=6)
    i = data.origins.get_loc(ORIGIN)
    # Starts before the fold end, target window (origin+30, origin+90] ends after it.
    assert not data.rows("q21", 60, "all", between=(start, end))[i]
    inside = data.origins.get_loc(ORIGIN - pd.Timedelta(minutes=90))
    assert data.rows("q21", 60, "all", between=(start, end))[inside]
    # Every selected row of every horizon ends strictly before the fold end.
    for horizon in tsf.STAGE1_HORIZONS:
        mask = data.rows("q21", horizon, "all", between=(start, end))
        assert (data.window_end[horizon][mask] < end).all()


def test_crossing_report_counts_removed_rows():
    data = _dynamics()
    folds = {"f": (ORIGIN - pd.Timedelta(hours=6), ORIGIN)}
    report = tsf.crossing_report(data, folds)
    by_h = {r["horizon"]: r["removed_rows"] for r in report if r["analyser"] == "q21"}
    # 30-minute grid: a window of h+30 minutes crosses the end for (h+30)/30 origins.
    assert by_h == {60: 3, 120: 5, 180: 7}


# ---------------------------------------------------------------------------
# 2. quiet_future checks the whole control path
# ---------------------------------------------------------------------------

def _t6_with(changes):
    t6 = pd.Series(350.0, index=GRID)
    for start, stop, value in changes:
        t6.loc[(GRID >= start) & (GRID < stop)] = value
    return t6


def test_quiet_rejects_intermediate_round_trip():
    t6 = _t6_with([(ORIGIN + pd.Timedelta(minutes=10), ORIGIN + pd.Timedelta(minutes=50), 360.0)])
    assert t6[ORIGIN] == 350 and t6[ORIGIN + pd.Timedelta(minutes=90)] == 350
    assert not _quiet(_dynamics(t6=t6))


def test_quiet_rejects_two_small_steps_adding_up():
    t6 = _t6_with([(ORIGIN + pd.Timedelta(minutes=20), GRID[-1] + pd.Timedelta(minutes=1), 350.6)])
    t6.loc[t6.index >= ORIGIN + pd.Timedelta(minutes=50)] = 351.2
    assert not _quiet(_dynamics(t6=t6))


def test_quiet_accepts_noise_within_tolerance_and_constant_path():
    rng = np.random.default_rng(0)
    t6 = pd.Series(350.0 + rng.uniform(-0.4, 0.4, len(GRID)), index=GRID)
    assert _quiet(_dynamics(t6=t6))
    assert _quiet(_dynamics())


def test_quiet_rejects_missing_path():
    t6 = pd.Series(350.0, index=GRID)
    gap = (GRID > ORIGIN + pd.Timedelta(minutes=10)) & (GRID < ORIGIN + pd.Timedelta(minutes=60))
    assert not _quiet(_dynamics(t6=t6[~gap]))
    # A missing tail (last reading more than 30 min before the window end) is unknown too.
    tail = GRID > ORIGIN + pd.Timedelta(minutes=50)
    t6_tail = pd.concat([t6[~tail], pd.Series(350.0, index=GRID[GRID > ORIGIN + pd.Timedelta(hours=5)])])
    assert not _quiet(_dynamics(t6=t6_tail))


def test_quiet_step_at_window_boundaries():
    window_end = ORIGIN + pd.Timedelta(minutes=90)
    at_end = _t6_with([(window_end, GRID[-1] + pd.Timedelta(minutes=1), 355.0)])
    assert not _quiet(_dynamics(t6=at_end))
    after_end = _t6_with([(window_end + pd.Timedelta(minutes=10), GRID[-1] + pd.Timedelta(minutes=1), 355.0)])
    assert _quiet(_dynamics(t6=after_end))
    # A step completed at the origin is in-flight history, not a future move.
    at_origin = _t6_with([(ORIGIN, GRID[-1] + pd.Timedelta(minutes=1), 355.0)])
    assert _quiet(_dynamics(t6=at_origin))


def test_quiet_checks_feed_and_pressure_paths():
    f9 = pd.Series(180.0, index=GRID)
    f9.loc[(GRID > ORIGIN) & (GRID < ORIGIN + pd.Timedelta(minutes=40))] = 190.0
    assert not _quiet(_dynamics(f9=f9))
    p13 = pd.Series(5.0, index=GRID)
    p13.loc[(GRID > ORIGIN) & (GRID < ORIGIN + pd.Timedelta(minutes=40))] = 5.2
    assert not _quiet(_dynamics(p13=p13))


def test_quiet_treats_flatline_control_readings_as_missing():
    f9 = pd.Series(180.0, index=GRID)
    flagged = pd.Series(False, index=GRID)
    flagged.loc[(GRID > ORIGIN) & (GRID <= ORIGIN + pd.Timedelta(minutes=90))] = True
    analyser = pd.Series(8.0 + 0.1 * np.sin(np.arange(len(GRID))), index=GRID)
    data = tsf.DynamicsData({"q21": analyser}, _controls(f9=f9), control_invalid={"F9": flagged})
    assert not _quiet(data)


def test_future_path_quiet_log_mode():
    times = pd.date_range("2025-01-01", periods=10, freq="10min")
    series = pd.Series([100.0] * 3 + [101.5] + [100.0] * 6, index=times)
    origin = np.array([times[1].value])
    end = np.array([times[6].value])
    assert future_path_quiet(series, origin, end, 0.02, log=True)[0]
    series.iloc[3] = 103.0
    assert not future_path_quiet(series, origin, end, 0.02, log=True)[0]


# ---------------------------------------------------------------------------
# 5. Future-mutation invariance of every feature family
# ---------------------------------------------------------------------------

def _mutate_after(series: pd.Series, cutoff, factor=3.0) -> pd.Series:
    out = series.copy()
    out[out.index > cutoff] = out[out.index > cutoff] * factor + 1.0
    return out


def test_analyser_cleaning_is_causal():
    rng = np.random.default_rng(1)
    series = pd.Series(8 + rng.normal(0, 0.2, len(GRID)), index=GRID)
    series.iloc[200:215] = 7.0  # a plateau spanning the cut
    for cut in (GRID[205], ORIGIN):
        full = clean_analyser(series)
        truncated = clean_analyser(series[series.index <= cut])
        pd.testing.assert_series_equal(full[full.index <= cut], truncated)


def test_dynamics_and_lab_features_ignore_future_mutations():
    rng = np.random.default_rng(2)
    analysers = {"q21": pd.Series(8 + rng.normal(0, 0.3, len(GRID)), index=GRID),
                 "pak": pd.Series(8.5 + rng.normal(0, 0.3, len(GRID)), index=GRID)}
    controls = _controls(t6=pd.Series(350 + rng.normal(0, 1, len(GRID)), index=GRID))
    labs = pd.DataFrame({"target_time": pd.date_range("2025-01-01T01:00", periods=24, freq="3h")})
    labs["target"] = 8 + rng.normal(0, 0.5, len(labs))
    origins = pd.DatetimeIndex([ORIGIN])
    before = build_features(analysers, controls, labs, origins, 240)
    mutated_a = {k: _mutate_after(v, ORIGIN) for k, v in analysers.items()}
    mutated_c = {k: _mutate_after(v, ORIGIN) for k, v in controls.items()}
    # Labs sampled before the origin but published after it are also future information.
    mutated_labs = labs.copy()
    unpublished = mutated_labs.target_time + pd.Timedelta(minutes=240) > ORIGIN
    assert unpublished.any() and (mutated_labs.target_time[unpublished] <= ORIGIN).any()
    mutated_labs.loc[unpublished, "target"] = 50.0
    after = build_features(mutated_a, mutated_c, mutated_labs, origins, 240)
    pd.testing.assert_frame_equal(before, after)
    pd.testing.assert_frame_equal(build_dynamics_features(analysers, controls, origins),
                                  build_dynamics_features(mutated_a, mutated_c, origins))


def test_stage1_fit_ignores_data_after_cutoff():
    rng = np.random.default_rng(3)
    analyser = pd.Series(np.exp(np.log(8) + np.cumsum(rng.normal(0, 0.01, len(GRID)))), index=GRID)
    controls = _controls(t6=pd.Series(350 + np.cumsum(rng.normal(0, 0.2, len(GRID))), index=GRID))
    cutoff = ORIGIN + pd.Timedelta(hours=6)
    base = tsf.DynamicsData({"q21": analyser, "pak": analyser * 1.05}, controls)
    changed = tsf.DynamicsData({"q21": _mutate_after(analyser, cutoff), "pak": _mutate_after(analyser * 1.05, cutoff)},
                               {k: _mutate_after(v, cutoff) for k, v in controls.items()})
    for training_filter in ("all", "quiet_future"):
        a = tsf.fit_stage1(base, "q21", 120, training_filter, 30.0, cutoff).artifact()
        b = tsf.fit_stage1(changed, "q21", 120, training_filter, 30.0, cutoff).artifact()
        assert a == b


# ---------------------------------------------------------------------------
# 3/4. Nested calibration and fold-local support on a synthetic plant
# ---------------------------------------------------------------------------

DAY = pd.Timedelta(days=1)
SYN_START = pd.Timestamp("2024-01-01")
SYN_FOLDS = {"A": (SYN_START + 40 * DAY, SYN_START + 70 * DAY), "B": (SYN_START + 70 * DAY, SYN_START + 100 * DAY),
             "C": (SYN_START + 100 * DAY, SYN_START + 130 * DAY)}


def synthetic_plant(days=150, seed=5):
    rng = np.random.default_rng(seed)
    index = pd.date_range(SYN_START, periods=days * 144, freq="10min")
    # Bounded set-point moves every 12 h (quiet stretches and interventions), small sensor noise.
    t6 = 350 + np.repeat(rng.normal(0, 3, days * 2), 72)[:len(index)] + rng.normal(0, 0.1, len(index))
    f9 = 180 * np.exp(np.repeat(rng.normal(0, 0.03, days), 144)[:len(index)])
    p13 = 5 + rng.normal(0, 0.005, len(index))
    drift = np.cumsum(rng.normal(0, 0.004, len(index)))
    ln_s = np.log(8) - 0.03 * (t6 - 350) + drift - 0.8 * drift.mean()
    truth = pd.Series(np.exp(ln_s), index=index)
    analysers = {"q21": pd.Series(truth * 0.9 + rng.normal(0, 0.1, len(index)), index=index),
                 "pak": pd.Series(truth * 1.1 + rng.normal(0, 0.1, len(index)), index=index)}
    controls = {"T6": pd.Series(t6, index=index), "F9": pd.Series(f9, index=index), "P13": pd.Series(p13, index=index)}
    sample_times = pd.date_range(SYN_START + pd.Timedelta(hours=10), periods=days * 2 - 1, freq="12h")
    labs = pd.DataFrame({"target_time": sample_times,
                         "target": truth.reindex(sample_times).to_numpy() * np.exp(rng.normal(0, 0.08, len(sample_times)))})
    return analysers, controls, labs


def _mutate_labels_from(labs, cutoff, delay=240):
    out = labs.copy()
    later = out.target_time + pd.Timedelta(minutes=delay) >= cutoff
    out.loc[later, "target"] = out.loc[later, "target"] * 2.5 + 3
    return out


@pytest.fixture(scope="module")
def plant():
    return synthetic_plant()


def _dump(model_set):
    return json.dumps(tsf.strip_diagnostics(model_set), sort_keys=True, default=tsf._json_default)


def test_fit_procedure_ignores_everything_after_cutoff(plant):
    analysers, controls, labs = plant
    cutoff = SYN_FOLDS["C"][0]
    folds = {k: v for k, v in SYN_FOLDS.items() if v[1] <= cutoff}
    base = tsf.build_context(analysers, controls, labs, 240)
    changed = tsf.build_context({k: _mutate_after(v, cutoff - pd.Timedelta(minutes=1)) for k, v in analysers.items()},
                                {k: _mutate_after(v, cutoff - pd.Timedelta(minutes=1)) for k, v in controls.items()},
                                _mutate_labels_from(labs, cutoff), 240)
    assert _dump(tsf.fit_procedure(base, cutoff, folds)) == _dump(tsf.fit_procedure(changed, cutoff, folds))


def test_outer_fold_model_set_does_not_see_outer_labels(plant):
    """Configuration, support and coefficients of fold B's model set ignore labels published from B on.

    (Predictions *inside* fold B legitimately use labels published before
    each origin as features, so only the fitted model set is compared.)
    """
    analysers, controls, labs = plant
    ctx = tsf.build_context(analysers, controls, labs, 240)
    start = SYN_FOLDS["B"][0]
    changed = tsf.build_context(analysers, controls, _mutate_labels_from(labs, start), 240)
    inner = {"A": SYN_FOLDS["A"]}
    a, b = tsf.fit_procedure(ctx, start, inner), tsf.fit_procedure(changed, start, inner)
    assert a["stage2"]["60"]["selection"]["basis"] == "earlier_folds"
    assert _dump(a) == _dump(b)


def test_first_fold_uses_preregistered_configuration(plant):
    ctx = tsf.build_context(*plant, 240)
    model_set = tsf.fit_procedure(ctx, SYN_FOLDS["A"][0], {})
    for horizon in ("0", "60", "120", "180"):
        assert model_set["stage2"][horizon]["kind"] == tsf.PREREGISTERED_STAGE2["kind"]
        assert model_set["stage2"][horizon]["selection"]["basis"] == "preregistered"
    for name in ("q21", "pak"):
        choice = model_set["stage1_selection"][name]["60"]
        assert (choice["training_filter"], choice["alpha"]) == (tsf.PREREGISTERED_STAGE1["training_filter"],
                                                                 tsf.PREREGISTERED_STAGE1["alpha"])


def test_residual_quantiles_come_only_from_outer_predictions(plant):
    ctx = tsf.build_context(*plant, 240)
    cutoff = SYN_FOLDS["C"][0]
    folds = {k: v for k, v in SYN_FOLDS.items() if v[1] <= cutoff}
    model_set = tsf.fit_procedure(ctx, cutoff, folds)
    residuals = []
    for name, (start, end) in folds.items():
        prior = {k: v for k, v in folds.items() if v[1] <= start}
        outer = tsf.fit_procedure(ctx, start, prior)
        rows = ctx.fold_rows(120, start, end)
        pred = tsf.predict_rows(ctx, outer, 120, rows)
        ok = pred.forecast_status.eq("ok") & pred.path.eq("stage2")
        residuals.append(np.log(np.maximum(pred.target[ok], 0.3)) - pred.ln_prediction[ok])
    expected = tsf.residual_quantile_function(np.concatenate(residuals))
    assert model_set["stage2"]["120"]["residual_quantiles"]["count"] == expected["count"]
    np.testing.assert_allclose(model_set["stage2"]["120"]["residual_quantiles"]["quantiles"], expected["quantiles"])
    assert model_set["stage2"]["120"]["alarm_probability"] == tsf.ALARM_PROBABILITY


def test_fold_rows_require_publication_before_fold_end(plant):
    ctx = tsf.build_context(*plant, 240)
    start, end = SYN_FOLDS["A"]
    for horizon in (0, 180):
        rows = ctx.fold_rows(horizon, start, end)
        frame = ctx.lab_frames[horizon]
        assert (pd.to_datetime(frame.loc[rows, "available_at"]) < end).all()
        assert (pd.to_datetime(frame.loc[rows, "prediction_origin"]) >= start).all()
