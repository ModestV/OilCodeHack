"""Shared, past-only features and applicability gates for sulphur inference.

An origin is the moment the prediction is issued. The forecast horizon and
laboratory publication delay are independent clocks. Rolling windows end at the
exact origin (also between native telemetry samples), never at a rounded grid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FORECAST_HORIZON_MINUTES = 180
LIMS_PUBLICATION_DELAY_MINUTES = 240
TELEMETRY_TOLERANCE_MINUTES = 30
MAX_LAB_AGE_MINUTES = 48 * 60
APPLICABILITY_POLICY = {
    "max_missing_fraction": 0.20,
    "max_ood_fraction": 0.10,
    "max_absolute_z": 12.0,
    "support_quantiles": [0.005, 0.995],
    "support_margin_fraction": 0.10,
    "description": "Engineering abstention heuristics, not calibrated confidence or safe control limits",
}


def telemetry_features(telemetry: pd.DataFrame, origins: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    """Current valid value and (origin-window, origin] means, with 30m freshness.

    Invalid readings must be NaN on entry. Every channel uses its own last
    finite observation, both offline and online. No forward interpolation.
    """
    telemetry = telemetry.sort_index()
    origin_ns = pd.DatetimeIndex(origins).as_unit("ns").asi8
    tolerance = pd.Timedelta(minutes=TELEMETRY_TOLERANCE_MINUTES).value
    result = {}
    latest_used = np.full(len(origins), np.iinfo(np.int64).min, dtype=np.int64)
    for column in telemetry.columns:
        values = telemetry[column].to_numpy(dtype=float)
        valid = np.isfinite(values)
        times = telemetry.index.as_unit("ns").asi8[valid]
        values = values[valid]
        right = np.searchsorted(times, origin_ns, side="right")
        current = np.full(len(origins), np.nan)
        fresh = right > 0
        if len(times):
            newest = times[np.maximum(right - 1, 0)]
            fresh &= origin_ns - newest <= tolerance
            current[fresh] = values[right[fresh] - 1]
            latest_used[fresh] = np.maximum(latest_used[fresh], newest[fresh])
        result[column] = current
        cumulative = np.concatenate(([0.0], np.cumsum(values)))
        for hours in (1, 6):
            left = np.searchsorted(times, origin_ns - pd.Timedelta(hours=hours).value, side="right")
            count = right - left
            means = np.full(len(origins), np.nan)
            usable = fresh & (count > 0)
            means[usable] = (cumulative[right[usable]] - cumulative[left[usable]]) / count[usable]
            result[f"{column}__mean_{hours}h"] = means
    # Stable order preserves all current features before the two window groups.
    columns = list(telemetry.columns)
    columns += [f"{c}__mean_1h" for c in telemetry.columns]
    columns += [f"{c}__mean_6h" for c in telemetry.columns]
    frame = pd.DataFrame(result, columns=columns)
    return frame, pd.Series(pd.to_datetime(latest_used))


def available_lab(target: pd.DataFrame, origins: pd.DatetimeIndex,
                  publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> pd.DataFrame:
    """Last finite nonnegative result whose sample+publication_delay <= origin."""
    ordered = target.sort_values("target_time").copy()
    ordered = ordered[np.isfinite(ordered["target"]) & (ordered["target"] >= 0)]
    ordered["previous_lab_sample_time"] = pd.to_datetime(ordered["target_time"])
    ordered["previous_lab_available_at"] = ordered["previous_lab_sample_time"] + pd.Timedelta(minutes=publication_delay_minutes)
    ordered = ordered.rename(columns={"target": "previous_lab_available"})
    joined = pd.merge_asof(
        pd.DataFrame({"prediction_origin": pd.DatetimeIndex(origins), "_order": np.arange(len(origins))}).sort_values("prediction_origin"),
        ordered[["previous_lab_sample_time", "previous_lab_available_at", "previous_lab_available"]],
        left_on="prediction_origin", right_on="previous_lab_available_at", direction="backward",
        tolerance=pd.Timedelta(minutes=MAX_LAB_AGE_MINUTES),
    ).sort_values("_order").reset_index(drop=True)
    return joined.drop(columns=["_order", "prediction_origin"])


def applicability(raw: np.ndarray, artifact: dict) -> dict:
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
    telemetry = np.array([c != "previous_lab_available" for c in artifact["feature_columns"]])
    reasons = []
    if not (finite & telemetry).any():
        reasons.append("no_telemetry_evidence")
    if missing_fraction > policy["max_missing_fraction"]:
        reasons.append("too_many_missing_features")
    if ood_fraction > policy["max_ood_fraction"] or (max_abs_z is not None and max_abs_z > policy["max_absolute_z"]):
        reasons.append("outside_training_support")
    return {
        "status": "abstain" if reasons else "ok", "reasons": reasons,
        "missing_fraction": missing_fraction, "ood_fraction": ood_fraction,
        "ood_feature_count": int(ood.sum()), "max_absolute_z": max_abs_z,
        "policy": policy,
    }
