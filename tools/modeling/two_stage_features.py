"""Shared, past-only features for the repaired two-stage sulphur forecast (v4r).

Ported unchanged from ``feat/forecast-v4-contour-v2@2397769``
(``tools/modeling/sulfur_features.py``) except for ``future_path_quiet``,
which replaces the endpoint-only quiet check of the audited v4.

An origin is the moment the prediction is issued.  Every feature is built only
from observations that are already known at the origin:

* telemetry and online analysers with ``timestamp <= origin``;
* laboratory results with ``sample_time + publication_delay <= origin``.

The forecast horizon and the laboratory publication delay are independent
clocks.  Rolling windows end at the exact origin, never at a rounded grid.

Design (see reports/review/v4-repair-2026-09-22/README.md):

* Stage 1 ("dynamics"): the online analysers (KIP ``Q21`` and the PAK export)
  run every 10 minutes.  Their change over the next 60/120/180 minutes is
  predicted from their own recent history and from the recent moves of the
  three control tags (``T6``, ``F9``, ``P13``), in the raw (un-anchored)
  ln space of each analyser.
* Lab anchoring: the daily LIMS sample is the control fact.  The analyser
  offset to the lab drifts by up to +-1 mg/kg between quarters, so it is
  re-estimated from the last published lab/analyser pairs and added to the
  raw analyser (now or predicted at ``origin + horizon``).
* Stage 2 ("calibration"): a convex (or linear) combination of the anchored
  analyser estimates, the local lab level and the previous published lab
  gives the ln sulphur estimate whose residuals define intervals and P(>10).
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
ANALYSER_TARGET_HALF_WINDOW_MINUTES = 30
LOG_FLOOR = 0.3

ANALYSER_METRICS = {"q21": "ht.Q21", "pak": "pak.ht.Mg.Sulfur"}
CONTROL_METRICS = {"T6": "ht.T6", "F9": "ht.F9", "P13": "ht.P13"}
# Normal-regime envelope for Stage 1 training rows (start-ups, shutdowns and
# upsets are excluded).  A documented prototype assumption, not a plant limit.
STAGE1_REGIME = {"T6": (320.0, 400.0), "F9": (100.0, None), "P13": (3.0, None)}
DELTA_WINDOWS_HOURS = (1.0, 3.0, 6.0)
# Stage 1 regression inputs: raw analyser history and control moves/levels.
DYNAMICS_COLUMNS = [
    "ln_q21_raw", "ln_q21_raw_1h", "ln_q21_raw_6h", "ln_q21_raw_24h",
    "ln_pak_raw", "ln_pak_raw_1h", "ln_pak_raw_6h", "ln_pak_raw_24h",
    "dT6_1h", "dT6_3h", "dT6_6h", "dlnF9_1h", "dlnF9_3h", "dlnF9_6h", "dP13_1h", "dP13_3h", "dP13_6h",
    "T6", "F9", "P13",
]
CONTROL_DELTA_COLUMNS = [c for c in DYNAMICS_COLUMNS if c.startswith("d")]
# Applicability (regime) gate: anchored evidence, lab anchor, control moves and levels.
SUPPORT_COLUMNS = [
    "ln_q21", "ln_q21_1h", "ln_pak", "ln_pak_1h", "ln_level", "ln_previous_lab",
    "ln_q21_24h", "ln_pak_24h",
    *CONTROL_DELTA_COLUMNS,
    "T6", "F9", "P13",
]
# Stage 2 inputs per horizon: at the origin the anchored analysers themselves,
# for h>0 the anchored Stage-1 predictions at origin+h.
STAGE2_INPUTS = {
    0: ["ln_q21", "ln_pak", "ln_level", "ln_previous_lab"],
    "h": ["ln_q21_pred", "ln_pak_pred", "ln_level", "ln_previous_lab"],
}
# Every column ``build_features`` produces before the anchor bookkeeping.
FEATURE_COLUMNS = list(dict.fromkeys([*SUPPORT_COLUMNS, *DYNAMICS_COLUMNS]))
# Kept for older callers; the v4 artifact stores column names explicitly.
MODEL_COLUMNS = STAGE2_INPUTS[0]
APPLICABILITY_POLICY = {
    "max_missing_fraction": 0.34,
    "max_ood_fraction": 0.20,
    "max_absolute_z": 8.0,
    "support_quantiles": [0.005, 0.995],
    "support_margin_fraction": 0.10,
    "min_anchor_pairs": 3,
    "description": "Engineering abstention heuristics, not calibrated confidence or safe control limits",
}
# Multivariate process-anomaly features of the hydro-treating reactor block.
# Level-invariant balances between related signals (the organisers' example:
# "temperatures normal on their own, but their balance unusual"), not the
# absolute operating point, which drifts with catalyst age.
ANOMALY_TAGS = ["ht.T6", "ht.T5", "ht.T11", "ht.P13", "ht.P8", "ht.F9", "ht.F14", "ht.F25"]
ANOMALY_FEATURES = {
    "R202_delta_T": ("ht.T11", "ht.T6", "difference"),      # reactor R-202 temperature rise
    "quench_delta_T": ("ht.T5", "ht.T6", "difference"),     # cooling between R-201 outlet and R-202 inlet
    "R202_delta_P": ("ht.P8", None, "value"),               # pressure drop across R-202
    "quench_per_feed": ("ht.F14", "ht.F9", "ratio"),        # quench flow per feed
    "hydrogen_per_feed": ("ht.F25", "ht.F9", "ratio"),      # fresh hydrogen per feed
    "P13": ("ht.P13", None, "value"),                       # reactor inlet pressure
}


def ln(values) -> np.ndarray:
    """Natural log with a floor; sulphur below 0.3 mg/kg is treated as 0.3."""
    array = np.asarray(values, dtype=float)
    out = np.full(array.shape, np.nan)
    finite = np.isfinite(array)
    out[finite] = np.log(np.maximum(array[finite], LOG_FLOOR))
    return out


def clean_analyser(series: pd.Series, flags: pd.Series | None = None) -> pd.Series:
    """Drop implausible analyser readings and frozen plateaus, causally.

    A reading is unusable when it is outside ``ANALYSER_RANGE``, carries a
    ``flatline`` flag from the importer, or has stayed within
    ``ANALYSER_PLATEAU_TOLERANCE`` of the previous readings for at least
    ``ANALYSER_PLATEAU_MINUTES`` *up to that reading* (a stuck or saturated
    analyser, e.g. the exact 307 plateau and the 24.87+-0.005 saturation in
    the source data).  The rule only looks backwards, so cleaning a series
    truncated at any time gives the same values as cleaning the full series
    and truncating afterwards; offline and runtime therefore agree.  Values
    are never interpolated.
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


def _sparse_table(values: np.ndarray, reduce) -> list[np.ndarray]:
    table = [values]
    width = 1
    while 2 * width <= len(values):
        previous = table[-1]
        table.append(reduce(previous[:-width], previous[width:]))
        width *= 2
    return table


def _range_reduce(table: list[np.ndarray], reduce, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """reduce(values[left:right]) per row; ``right > left`` is required."""
    length = right - left
    level = np.floor(np.log2(np.maximum(length, 1))).astype(int)
    out = np.empty(len(left))
    for k in np.unique(level):
        rows = level == k
        out[rows] = reduce(table[k][left[rows]], table[k][right[rows] - (1 << k)])
    return out


def future_path_quiet(series: pd.Series, origin_ns: np.ndarray, end_ns: np.ndarray, limit: float, *, log: bool = False,
                      max_gap_minutes: float = TELEMETRY_TOLERANCE_MINUTES) -> np.ndarray:
    """True where a control stays within ``limit`` of its origin value over the whole path ``(origin, end]``.

    Semantics (a training-row filter on the target window, never a runtime
    feature):

    * the reference is the value at the origin (as-of, at most
      ``max_gap_minutes`` old); a step at or before the origin is history;
    * every finite observation in ``(origin, end]`` must satisfy
      ``|x - reference| < limit`` (``ln`` values when ``log``), so a
      round trip 350 -> 360 -> 350 or two sub-limit steps adding up to the
      limit are interventions, a step exactly at ``end`` is too;
    * the path must be observed: no gap between consecutive observations
      (from the last one at/before the origin to the last one in the window)
      and no tail ``end - last`` longer than ``max_gap_minutes``.  Unknown is
      not quiet.  Noise within ``limit`` and constant values are quiet.

    ``series`` holds only readings the caller accepts (e.g. without
    importer ``flatline`` readings), NaN is treated as missing.
    """
    values = series.to_numpy(dtype=float)
    valid = np.isfinite(values)
    times = series.index.as_unit("ns").asi8[valid]
    values = values[valid]
    origin_ns = np.asarray(origin_ns, dtype=np.int64)
    end_ns = np.asarray(end_ns, dtype=np.int64)
    out = np.zeros(len(origin_ns), dtype=bool)
    if len(times) < 2:
        return out
    if log:
        values = ln(values)
    gap = pd.Timedelta(minutes=max_gap_minutes).value
    left = np.searchsorted(times, origin_ns, side="right")
    right = np.searchsorted(times, end_ns, side="right")
    has_reference = left > 0
    reference = np.full(len(origin_ns), np.nan)
    previous = np.maximum(left - 1, 0)
    fresh = has_reference & (origin_ns - times[previous] <= gap)
    reference[fresh] = values[previous[fresh]]
    candidate = fresh & (right > left)
    if not candidate.any():
        return out
    lo, hi = left[candidate], right[candidate]
    high = _range_reduce(_sparse_table(values, np.maximum), np.maximum, lo, hi)
    low = _range_reduce(_sparse_table(values, np.minimum), np.minimum, lo, hi)
    deviation = np.maximum(high - reference[candidate], reference[candidate] - low)
    diffs = np.diff(times).astype(float)
    # Gaps between consecutive observations from the one before the window to the last one in it.
    max_gap = _range_reduce(_sparse_table(diffs, np.maximum), np.maximum, lo - 1, hi - 1)
    tail = end_ns[candidate] - times[hi - 1]
    out[np.flatnonzero(candidate)] = (deviation < limit) & (max_gap <= gap) & (tail <= gap)
    return out


def analyser_target(series: pd.Series, origins: pd.DatetimeIndex, horizon_minutes: float) -> np.ndarray:
    """Analyser level around ``origin + horizon``: mean over +-30 minutes (>= 2 readings).

    The same window is used by ``lab_anchor`` to pair an analyser with a lab
    sample, so Stage 1 predicts exactly the quantity that is anchored.
    """
    origin_ns = pd.DatetimeIndex(origins).as_unit("ns").asi8
    centre = origin_ns + pd.Timedelta(minutes=horizon_minutes + ANALYSER_TARGET_HALF_WINDOW_MINUTES).value
    return _window_mean(series, centre, 2 * ANALYSER_TARGET_HALF_WINDOW_MINUTES / 60, 2)


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
        analyser_at_sample[name] = analyser_target(series, pd.DatetimeIndex(ordered["target_time"]), 0)
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


def build_dynamics_features(analysers: dict[str, pd.Series], controls: dict[str, pd.Series],
                            origins: pd.DatetimeIndex) -> pd.DataFrame:
    """Stage-1 inputs for a list of origins: raw analyser history, control moves and levels.

    No laboratory information is used, so the frame can be built on a dense
    grid.  Missing sources produce NaN columns.
    """
    origins = pd.DatetimeIndex(origins)
    origin_ns = origins.as_unit("ns").asi8
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    frame = pd.DataFrame(index=np.arange(len(origins)))
    for name in ANALYSER_METRICS:
        series = analysers.get(name, empty)
        frame[f"ln_{name}_raw"] = ln(_asof(series, origin_ns, TELEMETRY_TOLERANCE_MINUTES))
        frame[f"ln_{name}_raw_1h"] = ln(_window_mean(series, origin_ns, 1.0, 3))
        frame[f"ln_{name}_raw_6h"] = ln(_window_mean(series, origin_ns, 6.0, 18))
        frame[f"ln_{name}_raw_24h"] = ln(_window_mean(series, origin_ns, 24.0, 36))
    t6 = controls.get("T6", empty)
    f9 = controls.get("F9", empty)
    p13 = controls.get("P13", empty)
    for hours in DELTA_WINDOWS_HOURS:
        label = f"{int(hours)}h"
        frame[f"dT6_{label}"] = _delta(t6, origin_ns, hours)
        frame[f"dlnF9_{label}"] = _delta(f9, origin_ns, hours, log=True)
        frame[f"dP13_{label}"] = _delta(p13, origin_ns, hours)
    frame["T6"] = _asof(t6, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    frame["F9"] = _asof(f9, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    frame["P13"] = _asof(p13, origin_ns, TELEMETRY_TOLERANCE_MINUTES)
    return frame[DYNAMICS_COLUMNS]


def regime_mask(frame: pd.DataFrame) -> np.ndarray:
    """Rows inside the documented normal-regime envelope (``STAGE1_REGIME``)."""
    mask = np.ones(len(frame), dtype=bool)
    for name, (low, high) in STAGE1_REGIME.items():
        values = frame[name].to_numpy(dtype=float)
        ok = np.isfinite(values)
        if low is not None:
            ok &= values > low
        if high is not None:
            ok &= values < high
        mask &= ok
    return mask


def build_features(analysers: dict[str, pd.Series], controls: dict[str, pd.Series],
                   labs: pd.DataFrame, origins: pd.DatetimeIndex,
                   publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> pd.DataFrame:
    """Assemble the shared feature frame (dynamics + lab-anchored evidence) for a list of origins.

    ``analysers`` maps ``q21``/``pak`` to *cleaned* series; ``controls`` maps
    ``T6``/``F9``/``P13`` to telemetry series.  Missing sources produce NaN
    columns so offline and runtime frames have the same shape.
    """
    origins = pd.DatetimeIndex(origins)
    origin_ns = origins.as_unit("ns").asi8
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    frame = build_dynamics_features(analysers, controls, origins)
    anchor = lab_anchor(labs, {k: analysers.get(k, empty) for k in ANALYSER_METRICS}, origins, publication_delay_minutes)
    previous = available_lab(labs, origins, publication_delay_minutes)
    for name in ANALYSER_METRICS:
        series = analysers.get(name, empty)
        offset = anchor[f"offset_{name}"].to_numpy()
        frame[f"ln_{name}"] = ln(_asof(series, origin_ns, TELEMETRY_TOLERANCE_MINUTES) + offset)
        frame[f"ln_{name}_1h"] = ln(_window_mean(series, origin_ns, 1.0, 3) + offset)
        frame[f"ln_{name}_24h"] = ln(_window_mean(series, origin_ns, 24.0, 36) + offset)
    frame["ln_level"] = ln(anchor["level"].to_numpy())
    frame["ln_previous_lab"] = ln(previous["previous_lab_available"].to_numpy())
    frame = frame[FEATURE_COLUMNS]
    for column in anchor.columns:
        frame[column] = anchor[column].to_numpy()
    for column in previous.columns:
        frame[column] = previous[column].to_numpy()
    frame["prediction_origin"] = origins
    return frame


def predict_linear(model: dict, raw: np.ndarray) -> np.ndarray:
    """Standardized linear model from the artifact with train-only median imputation.

    ``raw`` is a 1-D or 2-D array ordered as ``model["feature_columns"]``.
    """
    raw = np.asarray(raw, dtype=float)
    filled = np.where(np.isfinite(raw), raw, np.asarray(model["medians"], dtype=float))
    standardized = (filled - np.asarray(model["mean"], dtype=float)) / np.asarray(model["scale"], dtype=float)
    coef = np.asarray(model["coef"], dtype=float)
    return standardized @ coef[1:] + coef[0]


def predict_convex(model: dict, inputs: np.ndarray) -> np.ndarray:
    """Convex combination of ln inputs; weights renormalised over the finite inputs.

    When every input that carries weight is missing but other evidence is
    finite, the finite inputs are averaged with equal weights (documented
    fallback: the lab level / previous lab still bound the estimate).
    """
    inputs = np.asarray(inputs, dtype=float)
    weights = np.asarray(model["weights"], dtype=float)
    single = inputs.ndim == 1
    inputs = np.atleast_2d(inputs)
    finite = np.isfinite(inputs)
    weighted = np.where(finite, inputs, 0.0) * weights
    mass = (finite * weights).sum(axis=1)
    out = np.full(len(inputs), np.nan)
    usable = mass > 0
    out[usable] = weighted[usable].sum(axis=1) / mass[usable]
    fallback = ~usable & finite.any(axis=1)
    if fallback.any():
        out[fallback] = np.where(finite[fallback], inputs[fallback], 0.0).sum(axis=1) / finite[fallback].sum(axis=1)
    out[np.isfinite(out)] += float(model.get("bias", 0.0))
    return out[0] if single else out


def stage2_predict(model: dict, inputs: np.ndarray) -> np.ndarray:
    """ln sulphur from Stage-2 inputs ordered as ``model["feature_columns"]``."""
    if model.get("kind") == "linear":
        return predict_linear(model, inputs)
    return predict_convex(model, inputs)


def stage1_predict(model: dict, raw: np.ndarray) -> np.ndarray:
    """Predicted change of raw ln analyser over the model horizon."""
    return predict_linear(model, raw)


def applicability(raw: np.ndarray, artifact: dict, anchor_pairs: int | None = None) -> dict:
    """Return explicit support evidence; thresholds are fixed before evaluation.

    ``raw`` is ordered as ``artifact["feature_columns"]`` (the support model).
    """
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
            "count": int(len(residuals)),
            "sigma": float(np.std(residuals)) if len(residuals) > 1 else None}


def widen_quantiles(quantile_function: dict, extra_sigma: float) -> dict:
    """Quantile function of the residual convolved with an independent N(0, extra_sigma^2).

    The empirical quantiles are stretched around their median by
    ``sqrt(1 + extra_sigma^2 / sigma^2)``: exact for Gaussian residuals,
    monotone and median-preserving otherwise.  ``extra_sigma`` is the ln
    uncertainty of a scenario effect (coefficient standard errors times the
    proposed move).
    """
    extra_sigma = float(extra_sigma or 0.0)
    sigma = quantile_function.get("sigma")
    if extra_sigma <= 0 or not sigma:
        return quantile_function
    probabilities = np.asarray(quantile_function["probabilities"], dtype=float)
    quantiles = np.asarray(quantile_function["quantiles"], dtype=float)
    median = float(np.interp(0.5, probabilities, quantiles))
    factor = float(np.sqrt(1.0 + (extra_sigma / sigma) ** 2))
    return {**quantile_function, "quantiles": (median + (quantiles - median) * factor).tolist(),
            "sigma": float(np.sqrt(sigma ** 2 + extra_sigma ** 2)), "widened_by_sigma": extra_sigma}


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


def anomaly_vector(values: dict[str, float | None], features: dict | None = None) -> dict[str, float]:
    """Derived anomaly features (``ANOMALY_FEATURES``) from raw tag values; NaN where inputs are missing."""
    features = features or ANOMALY_FEATURES
    out = {}
    for name, (a, b, kind) in features.items():
        x = values.get(a)
        y = values.get(b) if b else None
        x = np.asarray(x, dtype=float) if x is not None else np.nan
        y = np.asarray(y, dtype=float) if y is not None else np.nan
        if kind == "value":
            out[name] = x
        elif kind == "difference":
            out[name] = x - y
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                out[name] = np.where(np.isfinite(y) & (y != 0), x / np.where(y == 0, np.nan, y), np.nan)
                if np.ndim(out[name]) == 0:
                    out[name] = float(out[name])
    return out


def mahalanobis_score(model: dict, values: dict[str, float | None]) -> dict:
    """Robust Mahalanobis distance of the current reactor-block balance vector to the train regime.

    ``model`` holds ``tags`` (feature names), ``features`` (their
    definitions), ``location``, ``scale`` (robust per-feature std),
    ``precision`` (inverse robust covariance) and ``thresholds``
    (``attention`` = train 95% quantile, ``high`` = train 99% quantile of
    d^2).  The index is ``d^2 / high``; classes are a prototype proxy for an
    unusual combination of signals, not a failure probability.
    """
    tags = list(model["tags"])
    if model.get("features"):
        derived = anomaly_vector(values, {k: tuple(v) for k, v in model["features"].items()})
        vector = np.array([derived.get(tag, np.nan) for tag in tags], dtype=float)
    else:
        vector = np.array([values.get(tag) if values.get(tag) is not None else np.nan for tag in tags], dtype=float)
    if not np.isfinite(vector).all():
        missing = [tag for tag, value in zip(tags, vector) if not np.isfinite(value)]
        return {"status": "unavailable", "d2": None, "index": None, "class": None, "factors": [],
                "reason": f"Нет достоверных значений для индекса аномалии: {', '.join(missing)}"}
    location = np.asarray(model["location"], dtype=float)
    scale = np.asarray(model["scale"], dtype=float)
    precision = np.asarray(model["precision"], dtype=float)
    centred = vector - location
    d2 = float(centred @ precision @ centred)
    high = float(model["thresholds"]["high"])
    attention = float(model["thresholds"]["attention"])
    index = d2 / high if high > 0 else None
    contributions = centred * (precision @ centred)
    factors = [{"metric_id": tag, "value": float(v), "train_location": float(m), "robust_z": float(z),
                "contribution": float(c)} for tag, v, m, z, c in zip(tags, vector, location, centred / scale, contributions)]
    factors.sort(key=lambda f: -abs(f["contribution"]))
    klass = "high" if d2 > high else "attention" if d2 > attention else "normal"
    return {"status": "ok", "d2": d2, "index": index, "class": klass, "thresholds": {"attention": attention, "high": high},
            "factors": factors, "method": model.get("method"), "train_rows": model.get("train_rows")}
