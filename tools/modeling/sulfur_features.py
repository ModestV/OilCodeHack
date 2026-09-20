"""Shared, past-only features for hydro-treated diesel sulphur inference.

An origin is the moment the prediction is issued.  Every feature is built only
from observations that are already known at the origin:

* telemetry and online analysers with ``timestamp <= origin``;
* laboratory results with ``sample_time + publication_delay <= origin``.

The forecast horizon and the laboratory publication delay are independent
clocks.  Rolling windows end at the exact origin, never at a rounded grid.

Design (see reports/modeling/sulfur-forecast/REPORT.md):

* The daily LIMS sample is the control fact but arrives ~once per day.  The
  online analysers (KIP ``Q21`` and the PAK export) run every 10 minutes and
  track the lab result at sampling time (corr ~0.5-0.6), but their offset to the
  lab drifts by up to +-1 mg/kg between quarters.  The offset is therefore
  re-estimated from the last published lab/analyser pairs ("lab anchoring").
* Control moves (``T6``, ``F9``, ``P13``) reach the product with a lag of about
  1-2 hours; recent changes are exposed as separate features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS_MINUTES = (0, 60, 120, 180)
LIMS_PUBLICATION_DELAY_MINUTES = 240
TELEMETRY_TOLERANCE_MINUTES = 30
MAX_LAB_AGE_MINUTES = 48 * 60
LAB_ANCHOR_SAMPLES = 10
LAB_LEVEL_SAMPLES = 5
LAB_ANCHOR_MAX_AGE_DAYS = 45
ANALYSER_PLATEAU_MINUTES = 60
ANALYSER_PLATEAU_TOLERANCE = 0.01
ANALYSER_RANGE = (0.0, 100.0)
LOG_FLOOR = 0.3

ANALYSER_METRICS = {"q21": "ht.Q21", "pak": "pak.ht.Mg.Sulfur"}
CONTROL_METRICS = {"T6": "ht.T6", "F9": "ht.F9", "P13": "ht.P13"}
# Regression inputs: only product-sulphur evidence.  Absolute control levels
# and multi-day analyser means drift with catalyst age and analyser
# calibration, and learned coefficients on recent control moves mostly encode
# the operator's reaction to sulphur rather than the physical response; both
# degraded the 2026 hold-out, so they are excluded from the regression and
# kept only for the applicability (regime) gate and the scenario model.
MODEL_COLUMNS = ["ln_q21", "ln_q21_1h", "ln_pak", "ln_pak_1h", "ln_level", "ln_previous_lab"]
FEATURE_COLUMNS = [
    *MODEL_COLUMNS,
    "ln_q21_24h", "ln_pak_24h",
    "dT6_1h", "dT6_3h", "dlnF9_3h", "dP13_3h",
    "T6", "F9", "P13",
]
APPLICABILITY_POLICY = {
    "max_missing_fraction": 0.34,
    "max_ood_fraction": 0.20,
    "max_absolute_z": 8.0,
    "support_quantiles": [0.005, 0.995],
    "support_margin_fraction": 0.10,
    "min_anchor_pairs": 3,
    "description": "Engineering abstention heuristics, not calibrated confidence or safe control limits",
}


def ln(values) -> np.ndarray:
    """Natural log with a floor; sulphur below 0.3 mg/kg is treated as 0.3."""
    array = np.asarray(values, dtype=float)
    out = np.full(array.shape, np.nan)
    finite = np.isfinite(array)
    out[finite] = np.log(np.maximum(array[finite], LOG_FLOOR))
    return out


def clean_analyser(series: pd.Series, flags: pd.Series | None = None) -> pd.Series:
    """Drop implausible analyser readings and frozen plateaus.

    A reading is unusable when it is outside ``ANALYSER_RANGE``, carries a
    ``flatline`` flag from the importer, or stays within
    ``ANALYSER_PLATEAU_TOLERANCE`` of the previous reading for at least
    ``ANALYSER_PLATEAU_MINUTES`` (a stuck or saturated analyser, e.g. the exact
    307 plateau and the 24.87+-0.005 saturation in the source data).  Values are
    never interpolated.
    """
    values = series.sort_index().astype(float).copy()
    low, high = ANALYSER_RANGE
    values[(values <= low) | (values >= high)] = np.nan
    if flags is not None:
        flagged = flags.reindex(values.index).fillna("").astype(str).str.contains("flatline")
        values[flagged.to_numpy()] = np.nan
    same = ((values - values.shift()).abs() <= ANALYSER_PLATEAU_TOLERANCE) & values.notna()
    run_id = (~same).cumsum()
    run_start = values.index.to_series().groupby(run_id).transform("first")
    plateau_length = (values.index.to_series() - run_start).dt.total_seconds() / 60
    values[(plateau_length >= ANALYSER_PLATEAU_MINUTES).to_numpy() & same.to_numpy()] = np.nan
    # Also drop the first samples of a plateau that later proves to be stuck.
    stuck_runs = set(run_id[(plateau_length >= ANALYSER_PLATEAU_MINUTES) & same])
    values[run_id.isin(stuck_runs).to_numpy()] = np.nan
    return values


def _asof(series: pd.Series, origin_ns: np.ndarray, tolerance_minutes: float) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    valid = np.isfinite(values)
    times = series.index.as_unit("ns").asi8[valid]
    values = values[valid]
    out = np.full(len(origin_ns), np.nan)
    if not len(times):
        return out
    right = np.searchsorted(times, origin_ns, side="right")
    has = right > 0
    newest = times[np.maximum(right - 1, 0)]
    fresh = has & (origin_ns - newest <= pd.Timedelta(minutes=tolerance_minutes).value)
    out[fresh] = values[right[fresh] - 1]
    return out


def _window_mean(series: pd.Series, origin_ns: np.ndarray, hours: float, min_count: int) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    valid = np.isfinite(values)
    times = series.index.as_unit("ns").asi8[valid]
    values = values[valid]
    out = np.full(len(origin_ns), np.nan)
    if not len(times):
        return out
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    right = np.searchsorted(times, origin_ns, side="right")
    left = np.searchsorted(times, origin_ns - pd.Timedelta(hours=hours).value, side="right")
    count = right - left
    usable = count >= min_count
    out[usable] = (cumulative[right[usable]] - cumulative[left[usable]]) / count[usable]
    return out


def _delta(series: pd.Series, origin_ns: np.ndarray, hours: float, log: bool = False) -> np.ndarray:
    now = _asof(series, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    before = _asof(series, origin_ns - pd.Timedelta(hours=hours).value, TELEMETRY_TOLERANCE_MINUTES)
    if log:
        return ln(now) - ln(before)
    return now - before


def available_lab(target: pd.DataFrame, origins: pd.DatetimeIndex,
                  publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> pd.DataFrame:
    """Last finite nonnegative lab result whose ``sample + publication_delay <= origin``."""
    ordered = target.sort_values("target_time").copy()
    ordered = ordered[np.isfinite(ordered["target"]) & (ordered["target"] >= 0)]
    ordered["previous_lab_sample_time"] = pd.to_datetime(ordered["target_time"]).astype("datetime64[ns]")
    ordered["previous_lab_available_at"] = ordered["previous_lab_sample_time"] + pd.Timedelta(minutes=publication_delay_minutes)
    ordered = ordered.rename(columns={"target": "previous_lab_available"})
    left = pd.DataFrame({"prediction_origin": pd.DatetimeIndex(origins).astype("datetime64[ns]"),
                         "_order": np.arange(len(origins))}).sort_values("prediction_origin")
    joined = pd.merge_asof(
        left, ordered[["previous_lab_sample_time", "previous_lab_available_at", "previous_lab_available"]],
        left_on="prediction_origin", right_on="previous_lab_available_at", direction="backward",
        tolerance=pd.Timedelta(minutes=MAX_LAB_AGE_MINUTES),
    ).sort_values("_order").reset_index(drop=True)
    return joined.drop(columns=["_order", "prediction_origin"])


def lab_anchor(labs: pd.DataFrame, analysers: dict[str, pd.Series], origins: pd.DatetimeIndex,
               publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> pd.DataFrame:
    """Per-origin lab level and lab-minus-analyser offsets from published samples.

    ``labs`` has ``target_time``/``target``.  For every origin the last
    ``LAB_ANCHOR_SAMPLES`` samples published before the origin (and not older
    than ``LAB_ANCHOR_MAX_AGE_DAYS``) define:

    * ``level``: median of the last ``LAB_LEVEL_SAMPLES`` lab results;
    * ``offset_<analyser>``: median of ``lab - analyser`` where the analyser is
      averaged over +-30 minutes around the sampling time;
    * ``anchor_pairs_<analyser>``: number of usable pairs.
    """
    ordered = labs.sort_values("target_time")
    ordered = ordered[np.isfinite(ordered["target"]) & (ordered["target"] >= 0)]
    sample_ns = pd.DatetimeIndex(ordered["target_time"]).as_unit("ns").asi8
    lab_values = ordered["target"].to_numpy(dtype=float)
    available_ns = sample_ns + pd.Timedelta(minutes=publication_delay_minutes).value
    analyser_at_sample = {}
    for name, series in analysers.items():
        centre = _window_mean(series, sample_ns + pd.Timedelta(minutes=30).value, 1.0, 2)
        analyser_at_sample[name] = centre
    origin_ns = pd.DatetimeIndex(origins).as_unit("ns").asi8
    max_age = pd.Timedelta(days=LAB_ANCHOR_MAX_AGE_DAYS).value
    level = np.full(len(origin_ns), np.nan)
    offsets = {name: np.full(len(origin_ns), np.nan) for name in analysers}
    pairs = {name: np.zeros(len(origin_ns), dtype=int) for name in analysers}
    order = np.argsort(available_ns, kind="stable")
    available_sorted = available_ns[order]
    for i, origin in enumerate(origin_ns):
        right = np.searchsorted(available_sorted, origin, side="right")
        idx = order[:right]
        idx = idx[sample_ns[idx] >= origin - max_age]
        if not len(idx):
            continue
        idx = idx[np.argsort(sample_ns[idx], kind="stable")]
        recent = idx[-LAB_ANCHOR_SAMPLES:]
        level[i] = np.median(lab_values[idx[-LAB_LEVEL_SAMPLES:]])
        for name in analysers:
            diff = lab_values[recent] - analyser_at_sample[name][recent]
            diff = diff[np.isfinite(diff)]
            pairs[name][i] = len(diff)
            if len(diff):
                offsets[name][i] = np.median(diff)
    frame = pd.DataFrame({"level": level})
    for name in analysers:
        frame[f"offset_{name}"] = offsets[name]
        frame[f"anchor_pairs_{name}"] = pairs[name]
    return frame


def build_features(analysers: dict[str, pd.Series], controls: dict[str, pd.Series],
                   labs: pd.DataFrame, origins: pd.DatetimeIndex,
                   publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> pd.DataFrame:
    """Assemble the shared feature frame for a list of origins.

    ``analysers`` maps ``q21``/``pak`` to *cleaned* series; ``controls`` maps
    ``T6``/``F9``/``P13`` to telemetry series.  Missing sources produce NaN
    columns so offline and runtime frames have the same shape.
    """
    origins = pd.DatetimeIndex(origins)
    origin_ns = origins.as_unit("ns").asi8
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    anchor = lab_anchor(labs, {k: analysers.get(k, empty) for k in ANALYSER_METRICS}, origins, publication_delay_minutes)
    previous = available_lab(labs, origins, publication_delay_minutes)
    frame = pd.DataFrame(index=np.arange(len(origins)))
    for name in ANALYSER_METRICS:
        series = analysers.get(name, empty)
        offset = anchor[f"offset_{name}"].to_numpy()
        frame[f"ln_{name}"] = ln(_asof(series, origin_ns, TELEMETRY_TOLERANCE_MINUTES) + offset)
        frame[f"ln_{name}_1h"] = ln(_window_mean(series, origin_ns, 1.0, 3) + offset)
        frame[f"ln_{name}_24h"] = ln(_window_mean(series, origin_ns, 24.0, 36) + offset)
    frame["ln_level"] = ln(anchor["level"].to_numpy())
    frame["ln_previous_lab"] = ln(previous["previous_lab_available"].to_numpy())
    t6 = controls.get("T6", empty)
    f9 = controls.get("F9", empty)
    p13 = controls.get("P13", empty)
    frame["dT6_1h"] = _delta(t6, origin_ns, 1.0)
    frame["dT6_3h"] = _delta(t6, origin_ns, 3.0)
    frame["dlnF9_3h"] = _delta(f9, origin_ns, 3.0, log=True)
    frame["dP13_3h"] = _delta(p13, origin_ns, 3.0)
    frame["T6"] = _asof(t6, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    frame["F9"] = _asof(f9, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    frame["P13"] = _asof(p13, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    frame = frame[FEATURE_COLUMNS]
    for column in anchor.columns:
        frame[column] = anchor[column].to_numpy()
    for column in previous.columns:
        frame[column] = previous[column].to_numpy()
    frame["prediction_origin"] = origins
    return frame


def applicability(raw: np.ndarray, artifact: dict, anchor_pairs: int | None = None) -> dict:
    """Return explicit support evidence; thresholds are fixed before evaluation."""
    finite = np.isfinite(raw)
    missing_fraction = float((~finite).mean())
    lower = np.asarray(artifact["support_lower"], dtype=float)
    upper = np.asarray(artifact["support_upper"], dtype=float)
    ood = finite & ((raw < lower) | (raw > upper))
    ood_fraction = float(ood.sum() / max(1, finite.sum()))
    standardized = (raw - np.asarray(artifact["mean"])) / np.asarray(artifact["scale"])
    max_abs_z = float(np.max(np.abs(standardized[finite]))) if finite.any() else None
    policy = artifact["applicability_policy"]
    columns = list(artifact["feature_columns"])
    reasons = []
    evidence = [c for c in ("ln_q21", "ln_pak", "ln_previous_lab", "ln_level") if c in columns]
    if not any(finite[columns.index(c)] for c in evidence):
        reasons.append("no_sulfur_evidence")
    if not any(finite[columns.index(c)] for c in ("T6", "F9", "P13") if c in columns):
        reasons.append("no_control_telemetry")
    if missing_fraction > policy["max_missing_fraction"]:
        reasons.append("too_many_missing_features")
    # Any control outside its training range means a different regime
    # (start-up, shutdown, upset); one such tag is enough to abstain.
    control_ood = any(ood[columns.index(c)] for c in ("T6", "F9", "P13") if c in columns)
    if control_ood or ood_fraction > policy["max_ood_fraction"] or (max_abs_z is not None and max_abs_z > policy["max_absolute_z"]):
        reasons.append("outside_training_support")
    if anchor_pairs is not None and anchor_pairs < policy.get("min_anchor_pairs", 0):
        reasons.append("insufficient_lab_anchor")
    return {
        "status": "abstain" if reasons else "ok", "reasons": reasons,
        "missing_fraction": missing_fraction, "ood_fraction": ood_fraction,
        "ood_feature_count": int(ood.sum()), "max_absolute_z": max_abs_z,
        "policy": policy,
    }


def residual_quantile_function(residuals: np.ndarray, probabilities: np.ndarray | None = None) -> dict:
    """Empirical quantiles of log residuals (actual - predicted) for intervals and P(>limit)."""
    residuals = np.asarray(residuals, dtype=float)
    residuals = residuals[np.isfinite(residuals)]
    probabilities = np.linspace(0.01, 0.99, 99) if probabilities is None else np.asarray(probabilities)
    return {"probabilities": probabilities.tolist(),
            "quantiles": np.quantile(residuals, probabilities).tolist(),
            "count": int(len(residuals))}


def exceedance_probability(quantile_function: dict, ln_prediction: float, ln_limit: float) -> float:
    """P(actual > limit) = P(residual > ln_limit - ln_prediction) from the empirical residuals."""
    probabilities = np.asarray(quantile_function["probabilities"], dtype=float)
    quantiles = np.asarray(quantile_function["quantiles"], dtype=float)
    threshold = ln_limit - ln_prediction
    if threshold <= quantiles[0]:
        return float(1.0 - probabilities[0])
    if threshold >= quantiles[-1]:
        return float(1.0 - probabilities[-1])
    cdf = float(np.interp(threshold, quantiles, probabilities))
    return float(1.0 - cdf)


def interval(quantile_function: dict, ln_prediction: float, lower: float = 0.1, upper: float = 0.9) -> tuple[float, float]:
    probabilities = np.asarray(quantile_function["probabilities"], dtype=float)
    quantiles = np.asarray(quantile_function["quantiles"], dtype=float)
    low = float(np.interp(lower, probabilities, quantiles))
    high = float(np.interp(upper, probabilities, quantiles))
    return float(np.exp(ln_prediction + low)), float(np.exp(ln_prediction + high))
