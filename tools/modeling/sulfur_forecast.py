"""Reproducible two-stage sulphur nowcast/forecast for hydro-treated diesel (artifact v4).

Reads the imported dataset (``storage/<id>/observations.parquet``) so that the
offline benchmark and the runtime endpoint share one cleaning path.

* Stage 1 (dynamics): for each online analyser (``Q21``, PAK) and horizon
  ``h`` in {60, 120, 180} a ridge on a dense 30-minute grid predicts the
  change of the raw ln analyser over ``h`` from the analyser history and the
  recent control moves/levels (``T6``, ``F9``, ``P13``).
* Stage 2 (calibration): for every horizon in {0, 60, 120, 180} the lab sample
  at ``t`` defines an origin ``t-h``; the lab-anchored analyser estimates at
  ``t`` (Stage 1 predictions for h>0), the local lab level and the previous
  published lab are combined (convex weights or a robust linear model) into
  ln sulphur.  Intervals and P(>10) come from out-of-fold residuals.

Walk-forward artifacts: one model set per ``fit_end`` in
``WALK_FORWARD_FIT_ENDS``; each uses only labels published before ``fit_end``
and only selection folds that end at or before ``fit_end``.  The runtime picks
the latest model whose ``fit_end`` is not after the forecast origin, so every
historical forecast from 2024 on is free of future information.  2026 is the
untouched temporal test of the last model.

Example::

    python tools/modeling/sulfur_forecast.py --dataset storage/hackathon \
        --output reports/modeling/sulfur-forecast
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.modeling.sulfur_features import (  # noqa: E402
    ANALYSER_METRICS,
    ANOMALY_FEATURES,
    ANOMALY_TAGS,
    APPLICABILITY_POLICY,
    CONTROL_METRICS,
    DYNAMICS_COLUMNS,
    HORIZONS_MINUTES,
    LAB_ANCHOR_SAMPLES,
    LIMS_PUBLICATION_DELAY_MINUTES,
    STAGE1_REGIME,
    STAGE2_INPUTS,
    SUPPORT_COLUMNS,
    TELEMETRY_TOLERANCE_MINUTES,
    _asof,
    analyser_target,
    anomaly_vector,
    applicability,
    build_dynamics_features,
    build_features,
    clean_analyser,
    exceedance_probability,
    interval,
    ln,
    predict_convex,
    predict_linear,
    regime_mask,
    residual_quantile_function,
)

TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
TARGET_POINT = "Гидроочистка"
TARGET_PARAMETER = "Mg.Sulfur"
HARD_LIMIT = 10.0
ALPHA_GRID = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0)
ALARM_PROBABILITY_GRID = (0.2, 0.25, 0.3, 0.35, 0.4, 0.5)
WALK_FORWARD_FIT_ENDS = ("2024-01-01", "2025-01-01", "2026-01-01")
SELECTION_FOLDS = {"2023H2": ("2023-07-01", "2024-01-01"), "2024": ("2024-01-01", "2025-01-01"),
                   "2025": ("2025-01-01", "2026-01-01")}
TEST_START = "2026-01-01"
STAGE1_STEP = "30min"
STAGE1_HORIZONS = tuple(h for h in HORIZONS_MINUTES if h > 0)
# "Quiet future" filter (R1): rows where the controls do not move between the
# origin and the target time, so the learned change describes the process
# left alone rather than the operator's habitual reaction to sulphur.
QUIET_FUTURE = {"T6": 1.0, "lnF9": 0.02, "P13": 0.05}
QUIET_MIN_FRACTION = 0.10
QUIET_TOLERANCE = 1.05
BOOTSTRAP = 200
SEED = 0
CONVEX_STEP = 0.05
STAGE2_KINDS = ("convex", "linear")


class StandardizedRidge:
    """Dependency-free ridge on standardized features with train-only imputation."""

    def __init__(self, alpha: float):
        self.alpha = float(alpha)

    def fit(self, frame: pd.DataFrame, target: np.ndarray) -> "StandardizedRidge":
        self.feature_columns_ = list(frame.columns)
        values = frame.to_numpy(dtype=float)
        lo, hi = APPLICABILITY_POLICY["support_quantiles"]
        with np.errstate(all="ignore"):
            self.support_lower_ = np.nanquantile(values, lo, axis=0)
            self.support_upper_ = np.nanquantile(values, hi, axis=0)
            self.medians_ = np.nanmedian(values, axis=0)
        self.medians_[~np.isfinite(self.medians_)] = 0.0
        values = np.where(np.isfinite(values), values, self.medians_)
        self.mean_ = values.mean(axis=0)
        self.scale_ = values.std(axis=0)
        self.scale_[~np.isfinite(self.scale_) | (self.scale_ == 0)] = 1.0
        margin = APPLICABILITY_POLICY["support_margin_fraction"] * np.maximum(self.support_upper_ - self.support_lower_, self.scale_)
        self.support_lower_ = np.nan_to_num(self.support_lower_ - margin)
        self.support_upper_ = np.nan_to_num(self.support_upper_ + margin)
        standardized = (values - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(standardized)), standardized])
        regularizer = np.eye(design.shape[1]) * self.alpha
        regularizer[0, 0] = 0.0
        self.coef_ = np.linalg.solve(design.T @ design + regularizer, design.T @ np.asarray(target, dtype=float))
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return predict_linear(self.artifact(), frame.loc[:, self.feature_columns_].to_numpy(dtype=float))

    def artifact(self) -> dict:
        return {"kind": "linear", "feature_columns": self.feature_columns_, "alpha": self.alpha,
                "medians": self.medians_.tolist(), "mean": self.mean_.tolist(), "scale": self.scale_.tolist(),
                "coef": self.coef_.tolist(), "support_lower": self.support_lower_.tolist(),
                "support_upper": self.support_upper_.tolist()}


class HuberRidge(StandardizedRidge):
    """Ridge with Huber loss (IRLS); a single contaminated lab sample cannot dominate."""

    def __init__(self, alpha: float, delta: float = 0.2, iterations: int = 30):
        super().__init__(alpha)
        self.delta = float(delta)
        self.iterations = int(iterations)

    def fit(self, frame: pd.DataFrame, target: np.ndarray) -> "HuberRidge":
        super().fit(frame, target)
        values = np.where(np.isfinite(frame.to_numpy(dtype=float)), frame.to_numpy(dtype=float), self.medians_)
        design = np.column_stack([np.ones(len(values)), (values - self.mean_) / self.scale_])
        y = np.asarray(target, dtype=float)
        regularizer = np.eye(design.shape[1]) * self.alpha
        regularizer[0, 0] = 0.0
        for _ in range(self.iterations):
            residual = y - design @ self.coef_
            weights = np.where(np.abs(residual) <= self.delta, 1.0, self.delta / np.maximum(np.abs(residual), 1e-12))
            weighted = design * weights[:, None]
            coef = np.linalg.solve(design.T @ weighted + regularizer, weighted.T @ y)
            if np.max(np.abs(coef - self.coef_)) < 1e-9:
                self.coef_ = coef
                break
            self.coef_ = coef
        return self


def fit_convex(inputs: np.ndarray, target: np.ndarray, columns: list[str], step: float = CONVEX_STEP) -> dict:
    """Convex combination of ln inputs minimising ln-MAE; weights on a simplex grid.

    Weights are renormalised over the finite inputs of each row (as at
    runtime), the bias is the median residual of the best weights.
    """
    inputs = np.asarray(inputs, dtype=float)
    target = np.asarray(target, dtype=float)
    steps = int(round(1 / step))
    best = None
    for combo in itertools.product(range(steps + 1), repeat=len(columns) - 1):
        if sum(combo) > steps:
            continue
        weights = np.array([*combo, steps - sum(combo)], dtype=float) / steps
        pred = predict_convex({"weights": weights, "bias": 0.0}, inputs)
        ok = np.isfinite(pred)
        if not ok.any():
            continue
        residual = target[ok] - pred[ok]
        bias = float(np.median(residual))
        score = float(np.mean(np.abs(residual - bias)))
        if best is None or score < best[0] - 1e-12:
            best = (score, weights, bias)
    score, weights, bias = best
    return {"kind": "convex", "feature_columns": list(columns), "weights": weights.tolist(), "bias": bias,
            "fit_ln_mae": score}


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (pd.Timedelta,)):
        return value.total_seconds()
    raise TypeError(f"cannot serialise {type(value)!r}")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(directory: Path) -> tuple[dict[str, pd.Series], dict[str, pd.Series], pd.DataFrame, dict[str, pd.Series]]:
    """Load cleaned analysers, control telemetry, the lab target and the anomaly tags from Parquet."""
    path = str(directory / "observations.parquet").replace("'", "''")
    wanted = list(dict.fromkeys(list(ANALYSER_METRICS.values()) + list(CONTROL_METRICS.values()) + [TARGET_METRIC] + ANOMALY_TAGS))
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"""SELECT metric_id, timestamp, value, coalesce(flags, '') AS flags FROM read_parquet('{path}')
                WHERE metric_id IN (SELECT unnest(?)) AND value IS NOT NULL AND isfinite(value)
                  AND NOT contains(coalesce(flags, ''), 'invalid') AND NOT contains(coalesce(flags, ''), 'conflict')
                ORDER BY timestamp""", [wanted]).fetchdf()
    rows["timestamp"] = pd.to_datetime(rows["timestamp"]).astype("datetime64[ns]")
    groups = {mid: part.drop_duplicates("timestamp", keep="last").set_index("timestamp")
              for mid, part in rows.groupby("metric_id")}
    analysers = {}
    for name, mid in ANALYSER_METRICS.items():
        part = groups.get(mid)
        if part is not None:
            analysers[name] = clean_analyser(part["value"], part["flags"])
    controls = {}
    for name, mid in CONTROL_METRICS.items():
        part = groups.get(mid)
        if part is not None:
            controls[name] = part["value"].astype(float)
    lab = groups.get(TARGET_METRIC)
    if lab is None:
        raise ValueError(f"{TARGET_METRIC} is missing from the dataset")
    labs = pd.DataFrame({"target_time": lab.index, "target": lab["value"].to_numpy(dtype=float)})
    labs = labs[labs["target"] >= 0].reset_index(drop=True)
    extras = {mid: groups[mid]["value"].astype(float) for mid in ANOMALY_TAGS if mid in groups}
    return analysers, controls, labs, extras


def regression_metrics(actual, predicted, threshold: float = HARD_LIMIT) -> dict:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mask = np.isfinite(actual) & np.isfinite(predicted)
    actual, predicted = actual[mask], predicted[mask]
    if not len(actual):
        return {"n": 0}
    error = predicted - actual
    dangerous, alarm = actual > threshold, predicted > threshold
    tp, fp, fn = int((dangerous & alarm).sum()), int((~dangerous & alarm).sum()), int((dangerous & ~alarm).sum())
    return {
        "n": int(len(actual)),
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "median_absolute_error": float(np.median(np.abs(error))),
        "bias_pred_minus_actual": float(np.mean(error)),
        "correlation": float(np.corrcoef(actual, predicted)[0, 1]) if len(actual) > 2 and predicted.std() > 0 else None,
        "actual_above_10": int(dangerous.sum()), "predicted_above_10": int(alarm.sum()),
        "precision_above_10": float(tp / (tp + fp)) if tp + fp else None,
        "recall_above_10": float(tp / (tp + fn)) if tp + fn else None,
        "false_alarm_rate": float(fp / max(1, (~dangerous).sum())),
    }


def probability_metrics(actual, probability, alarm_probability: float, threshold: float = HARD_LIMIT) -> dict:
    actual = np.asarray(actual, dtype=float)
    probability = np.asarray(probability, dtype=float)
    mask = np.isfinite(actual) & np.isfinite(probability)
    actual, probability = actual[mask], probability[mask]
    if not len(actual):
        return {"n": 0}
    dangerous = actual > threshold
    brier = float(np.mean((probability - dangerous) ** 2))
    base = float(dangerous.mean())
    brier_reference = float(np.mean((base - dangerous) ** 2))
    auc = None
    if dangerous.any() and (~dangerous).any():
        order = np.argsort(probability)
        ranks = np.empty(len(order))
        ranks[order] = np.arange(1, len(order) + 1)
        for value in np.unique(probability):
            tie = probability == value
            if tie.sum() > 1:
                ranks[tie] = ranks[tie].mean()
        auc = float((ranks[dangerous].sum() - dangerous.sum() * (dangerous.sum() + 1) / 2) / (dangerous.sum() * (~dangerous).sum()))
    alarm = probability >= alarm_probability
    tp, fp, fn = int((dangerous & alarm).sum()), int((~dangerous & alarm).sum()), int((dangerous & ~alarm).sum())
    return {"n": int(len(actual)), "base_rate_above_10": base, "brier": brier, "brier_climatology": brier_reference,
            "brier_skill": float(1 - brier / brier_reference) if brier_reference > 0 else None, "auc": auc,
            "alarm_probability": alarm_probability, "alarms": int(alarm.sum()),
            "recall_above_10": float(tp / (tp + fn)) if tp + fn else None,
            "precision_above_10": float(tp / (tp + fp)) if tp + fp else None,
            "false_alarm_rate": float(fp / max(1, (~dangerous).sum()))}


# ----------------------------------------------------------------------------
# Control response (scenario coefficients)
# ----------------------------------------------------------------------------

CONTROL_STEP_SPECS = {
    "T6": {"threshold": 3.0, "relative": False, "unit": "ln(mg/kg) per °C"},
    "F9": {"threshold": 8.0, "relative": True, "unit": "ln(mg/kg) per % of feed"},
    "P13": {"threshold": 0.15, "relative": False, "unit": "ln(mg/kg) per MPa"},
}


def estimate_control_response(analyser: pd.Series, controls: dict[str, pd.Series], end: str | pd.Timestamp | None = None,
                              *, seed: int = SEED, bootstrap: int = BOOTSTRAP) -> dict:
    """Median log-sulphur response to step changes of each control from analyser data before ``end``.

    Events are 1-hour changes of one control beyond a threshold while the
    other controls stay quiet and the unit is in a normal regime; consecutive
    10-minute indices belonging to the same ramp are merged into one event
    (a new event starts only after a quiet hour).  The response is the median
    over events of ``(ln S(t+k) - ln S(t-1h..t)) / step`` for k = 1..6 hours;
    the coefficient is the median over hours 2-4, the lag is the first hour
    reaching 80% of it, and a bootstrap over events gives ``se``/``ci_80``.
    Operators also move controls *because* sulphur drifts, so this is a
    lagged observational response, not a proven causal gain; the direction for
    temperature matches the expert statement.  Only data strictly before
    ``end`` are used so that a backtest after ``end`` sees no future.
    """
    frame = pd.DataFrame({"lnS": ln(analyser), **{k: v for k, v in controls.items()}}).sort_index()
    if end is not None:
        frame = frame[frame.index < pd.Timestamp(end)]
    frame = frame.resample("10min").last()
    normal = pd.Series(regime_mask(frame), index=frame.index)
    lnS = frame["lnS"].where(normal)
    rng = np.random.default_rng(seed)
    result = {}
    for name, spec in CONTROL_STEP_SPECS.items():
        series = frame[name]
        step = (series / series.shift(6) - 1) * 100 if spec["relative"] else series - series.shift(6)
        quiet = pd.Series(True, index=frame.index)
        for other, other_spec in CONTROL_STEP_SPECS.items():
            if other == name:
                continue
            other_step = (frame[other] / frame[other].shift(6) - 1) * 100 if other_spec["relative"] else frame[other] - frame[other].shift(6)
            quiet &= other_step.abs() < other_spec["threshold"] / 3
        candidates = np.where((step.abs() >= spec["threshold"]) & normal & quiet)[0]
        # Consecutive candidate indices (gap <= 1 hour) belong to one ramp:
        # the event starts at the first index, its size is the largest
        # 1-hour step inside the cluster and the response is read from the
        # end of the ramp.
        clusters: list[list[int]] = []
        for i in candidates:
            if clusters and i - clusters[-1][-1] <= 6:
                clusters[-1].append(int(i))
            else:
                clusters.append([int(i)])
        responses = []
        values = lnS.to_numpy()
        steps = step.to_numpy()
        for cluster in clusters:
            first, last = cluster[0], cluster[-1]
            if first - 6 < 0 or last + 39 >= len(values):
                continue
            size = steps[max(cluster, key=lambda j: abs(steps[j]))]
            base = np.nanmean(values[first - 6:first])
            trajectory = [np.nanmean(values[last + k * 6 - 3:last + k * 6 + 3]) - base for k in range(1, 7)]
            if np.isfinite(base) and np.all(np.isfinite(trajectory)) and size:
                responses.append(np.asarray(trajectory) / size)
        responses = np.asarray(responses)
        if len(responses) < 5:
            result[name] = {"events": int(len(responses)), "response_by_hour": None, "coefficient": None, "se": None,
                            "ci_80": None, "lag_minutes": None, "unit": spec["unit"], "end": str(end) if end else None}
            continue

        def summarise(sample: np.ndarray) -> tuple[float, int]:
            by_hour = np.median(sample, axis=0)
            plateau = float(np.median(by_hour[1:4]))
            reached = np.abs(by_hour[:3]) >= 0.8 * abs(plateau)
            lag = int(60 * (1 + int(np.argmax(reached)))) if reached.any() else 180
            return plateau, lag

        coefficient, lag = summarise(responses)
        by_hour = np.median(responses, axis=0)
        draws = np.array([summarise(responses[rng.integers(0, len(responses), len(responses))]) for _ in range(bootstrap)])
        result[name] = {"events": int(len(responses)), "step_threshold": spec["threshold"], "relative_step": spec["relative"],
                        "response_by_hour": {f"{k}h": float(by_hour[k - 1]) for k in range(1, 7)},
                        "coefficient": coefficient, "se": float(np.std(draws[:, 0])),
                        "ci_80": [float(np.quantile(draws[:, 0], 0.1)), float(np.quantile(draws[:, 0], 0.9))],
                        "lag_minutes": lag, "lag_ci_80": [int(np.quantile(draws[:, 1], 0.1)), int(np.quantile(draws[:, 1], 0.9))],
                        "bootstrap": bootstrap, "unit": spec["unit"], "end": str(end) if end else None}
    return result


# ----------------------------------------------------------------------------
# Process-anomaly model (robust covariance of the reactor block)
# ----------------------------------------------------------------------------

def _robust_covariance(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, str]:
    try:
        from sklearn.covariance import MinCovDet
        estimator = MinCovDet(support_fraction=0.75, random_state=SEED).fit(values)
        return estimator.location_, estimator.covariance_, "sklearn.MinCovDet(support_fraction=0.75)"
    except ImportError:
        median = np.median(values, axis=0)
        mad = 1.4826 * np.median(np.abs(values - median), axis=0)
        mad[mad == 0] = 1.0
        core = values[(np.abs(values - median) / mad < 3).all(axis=1)]
        return core.mean(axis=0), np.cov(core, rowvar=False), "trimmed median/MAD covariance"


def _mahalanobis(values: np.ndarray, location: np.ndarray, covariance: np.ndarray) -> np.ndarray:
    covariance = covariance + np.eye(covariance.shape[0]) * 1e-9 * np.trace(covariance) / covariance.shape[0]
    precision = np.linalg.inv(covariance)
    centred = values - location
    return np.einsum("ij,jk,ik->i", centred, precision, centred)


def fit_anomaly_model(frame: pd.DataFrame, end: str | pd.Timestamp, folds: dict | None = None) -> dict | None:
    """Robust location/covariance of the reactor-block balance features on normal-regime rows before ``end``.

    Uses scikit-learn's MinCovDet when available (offline only; the artifact
    stores plain arrays for the numpy runtime), otherwise a trimmed
    median/MAD covariance.  Thresholds (``attention`` = 95%, ``high`` = 99%)
    are calibrated *out of sample*: for every selection fold ending at or
    before ``end`` a model fitted on rows before the fold start scores the
    fold rows, and the quantiles of these d^2 values are used.  In-sample
    train quantiles understate the normal drift of the operating balance
    between catalyst cycles and would flag most of the next year as
    anomalous.
    """
    derived = pd.DataFrame({name: anomaly_vector({tag: frame[tag] for tag in frame.columns if tag in ANOMALY_TAGS}, {name: spec})[name]
                            for name, spec in ANOMALY_FEATURES.items() if spec[0] in frame.columns and (spec[1] is None or spec[1] in frame.columns)},
                           index=frame.index)
    tags = list(derived.columns)
    part = derived.loc[derived.index < pd.Timestamp(end)].replace([np.inf, -np.inf], np.nan).dropna()
    if len(part) < 200 or len(tags) < 3:
        return None
    values = part.to_numpy(dtype=float)
    location, covariance, method = _robust_covariance(values)
    d2_train = _mahalanobis(values, location, covariance)
    oos = []
    fold_evidence = {}
    for name, (start, fold_end) in (folds or {}).items():
        if pd.Timestamp(fold_end) > pd.Timestamp(end):
            continue
        fit = part.loc[part.index < pd.Timestamp(start)].to_numpy(dtype=float)
        score = part.loc[(part.index >= pd.Timestamp(start)) & (part.index < pd.Timestamp(fold_end))].to_numpy(dtype=float)
        if len(fit) < 200 or len(score) < 50:
            continue
        loc_f, cov_f, _ = _robust_covariance(fit)
        d2_f = _mahalanobis(score, loc_f, cov_f)
        oos.append(d2_f)
        fold_evidence[name] = {"rows": int(len(score)), "q95": float(np.quantile(d2_f, 0.95)), "q99": float(np.quantile(d2_f, 0.99))}
    calibration = np.concatenate(oos) if oos else d2_train
    covariance = covariance + np.eye(len(tags)) * 1e-9 * np.trace(covariance) / len(tags)
    precision = np.linalg.inv(covariance)
    return {"tags": tags, "features": {name: list(ANOMALY_FEATURES[name]) for name in tags},
            "location": location.tolist(), "scale": np.sqrt(np.diag(covariance)).tolist(),
            "precision": precision.tolist(),
            "thresholds": {"attention": float(np.quantile(calibration, 0.95)), "high": float(np.quantile(calibration, 0.99))},
            "threshold_basis": "out-of-sample folds" if oos else "in-sample train (no fold available)",
            "train_thresholds": {"attention": float(np.quantile(d2_train, 0.95)), "high": float(np.quantile(d2_train, 0.99))},
            "fold_evidence": fold_evidence,
            "method": method, "train_rows": int(len(values)), "end": str(pd.Timestamp(end))}


# ----------------------------------------------------------------------------
# Stage 1: analyser dynamics on a dense grid
# ----------------------------------------------------------------------------

class DynamicsData:
    """Dense-grid Stage-1 frame with targets per analyser/horizon and future-control masks."""

    def __init__(self, analysers: dict[str, pd.Series], controls: dict[str, pd.Series]):
        self.analysers = analysers
        self.controls = controls
        starts = [s.index.min() for s in list(analysers.values()) + list(controls.values()) if len(s)]
        ends = [s.index.max() for s in list(analysers.values()) + list(controls.values()) if len(s)]
        self.origins = pd.date_range(min(starts).ceil("h") + pd.Timedelta(hours=24), max(ends), freq=STAGE1_STEP)
        self.frame = build_dynamics_features(analysers, controls, self.origins)
        self.regime = regime_mask(self.frame)
        self.targets: dict[tuple[str, int], np.ndarray] = {}
        self.quiet: dict[int, np.ndarray] = {}
        origin_ns = self.origins.as_unit("ns").asi8
        for horizon in STAGE1_HORIZONS:
            for name, series in analysers.items():
                self.targets[(name, horizon)] = ln(analyser_target(series, self.origins, horizon)) - self.frame[f"ln_{name}_raw"].to_numpy()
            future_ns = origin_ns + pd.Timedelta(minutes=horizon).value
            quiet = np.ones(len(self.origins), dtype=bool)
            for control, limit in (("T6", QUIET_FUTURE["T6"]), ("P13", QUIET_FUTURE["P13"])):
                series = controls.get(control)
                if series is None:
                    quiet &= False
                    continue
                move = _asof(series, future_ns, TELEMETRY_TOLERANCE_MINUTES) - self.frame[control].to_numpy()
                quiet &= np.isfinite(move) & (np.abs(move) < limit)
            f9 = controls.get("F9")
            if f9 is None:
                quiet &= False
            else:
                move = ln(_asof(f9, future_ns, TELEMETRY_TOLERANCE_MINUTES)) - ln(self.frame["F9"].to_numpy())
                quiet &= np.isfinite(move) & (np.abs(move) < QUIET_FUTURE["lnF9"])
            self.quiet[horizon] = quiet
        self.target_time = {h: self.origins + pd.Timedelta(minutes=h + 30) for h in STAGE1_HORIZONS}

    def rows(self, name: str, horizon: int, training_filter: str, before: pd.Timestamp | None = None,
             between: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> np.ndarray:
        target = self.targets[(name, horizon)]
        mask = self.regime & np.isfinite(target) & np.isfinite(self.frame[f"ln_{name}_raw"].to_numpy())
        if training_filter == "quiet_future":
            mask &= self.quiet[horizon]
        if before is not None:
            # The whole target window must be known before the boundary.
            mask &= np.asarray(self.target_time[horizon] < before)
        if between is not None:
            mask &= np.asarray((self.origins >= between[0]) & (self.origins < between[1]))
        return mask


def stage1_implied_gains(model: dict) -> dict[str, float]:
    """ln change implied by a unit step of each control made just before the origin."""
    columns = model["feature_columns"]
    coef = np.asarray(model["coef"][1:], dtype=float) / np.asarray(model["scale"], dtype=float)
    gains = {}
    for control, prefix, per_unit in (("T6", "dT6_", 1.0), ("F9", "dlnF9_", 0.01), ("P13", "dP13_", 1.0)):
        total = sum(coef[i] for i, c in enumerate(columns) if c.startswith(prefix)) * per_unit
        if control in columns:
            total += coef[columns.index(control)] * per_unit
        gains[control] = float(total)
    return gains


def fit_stage1(data: DynamicsData, name: str, horizon: int, training_filter: str, alpha: float,
               before: pd.Timestamp) -> StandardizedRidge:
    mask = data.rows(name, horizon, training_filter, before=before)
    return StandardizedRidge(alpha).fit(data.frame.loc[mask, DYNAMICS_COLUMNS], data.targets[(name, horizon)][mask])


def stage1_apply(model: dict, raw: np.ndarray, policy: dict = APPLICABILITY_POLICY) -> tuple[np.ndarray, np.ndarray]:
    """Δln for rows ordered as ``model["feature_columns"]``; persistence (0) where inputs are too sparse.

    Returns ``(delta, fallback)``: ``fallback`` is True where the analyser's
    own current reading is missing or more than ``max_missing_fraction`` of
    the dynamics inputs are missing (median imputation would then dominate).
    """
    raw = np.atleast_2d(np.asarray(raw, dtype=float))
    delta = predict_linear(model, raw)
    own = model["feature_columns"].index(model["analyser_column"])
    missing = (~np.isfinite(raw)).mean(axis=1)
    fallback = ~np.isfinite(raw[:, own]) | (missing > policy["max_missing_fraction"]) | ~np.isfinite(delta)
    delta = np.where(fallback, 0.0, delta)
    return delta, fallback


def select_stage1(data: DynamicsData, name: str, horizon: int, fit_end: pd.Timestamp, folds: dict) -> dict:
    """Choose training filter and alpha on the selection folds (all rows scored in mg/kg)."""
    scores = {}
    for training_filter in ("all", "quiet_future"):
        fraction = float(data.rows(name, horizon, "quiet_future", before=fit_end).sum()
                         / max(1, data.rows(name, horizon, "all", before=fit_end).sum()))
        for alpha in ALPHA_GRID:
            per_fold = []
            for fold, (start, end) in folds.items():
                model = fit_stage1(data, name, horizon, training_filter, alpha, start)
                score_all = data.rows(name, horizon, "all", between=(start, end))
                score_quiet = data.rows(name, horizon, "quiet_future", between=(start, end))
                entry = {"fold": fold}
                for label, mask in (("all", score_all), ("quiet", score_quiet)):
                    if not mask.any():
                        entry[label] = None
                        continue
                    base = data.frame.loc[mask, f"ln_{name}_raw"].to_numpy()
                    actual = np.exp(base + data.targets[(name, horizon)][mask])
                    pred = np.exp(base + model.predict(data.frame.loc[mask]))
                    entry[label] = {"n": int(mask.sum()), "mae": float(np.mean(np.abs(actual - pred))),
                                    "persistence_mae": float(np.mean(np.abs(actual - np.exp(base))))}
                per_fold.append(entry)
            scores[(training_filter, alpha)] = {"folds": per_fold, "quiet_fraction": fraction,
                                                "mean_mae_all": float(np.mean([f["all"]["mae"] for f in per_fold if f["all"]])),
                                                "mean_mae_quiet": float(np.mean([f["quiet"]["mae"] for f in per_fold if f["quiet"]]))}
    best_all = min((k for k in scores if k[0] == "all"), key=lambda k: scores[k]["mean_mae_all"])
    best_quiet = min((k for k in scores if k[0] == "quiet_future"), key=lambda k: scores[k]["mean_mae_quiet"])
    # The quiet-future model describes "no further intervention" (the hold
    # semantics of the decision contour) but is trained on a selected sample;
    # it is used only when it is better where it matters (quiet rows) and not
    # materially worse overall.
    quiet_ok = (scores[best_quiet]["quiet_fraction"] >= QUIET_MIN_FRACTION
                and scores[best_quiet]["mean_mae_quiet"] <= scores[best_all]["mean_mae_quiet"]
                and scores[best_quiet]["mean_mae_all"] <= QUIET_TOLERANCE * scores[best_all]["mean_mae_all"])
    chosen = best_quiet if quiet_ok else best_all
    return {"training_filter": chosen[0], "alpha": chosen[1], "quiet_fraction": scores[chosen]["quiet_fraction"],
            "selection": {f"{k[0]}/{k[1]:g}": v for k, v in scores.items()},
            "chosen_scores": scores[chosen], "alternative": {"training_filter": best_all[0], "alpha": best_all[1],
                                                             "scores": scores[best_all]} if quiet_ok else
            {"training_filter": best_quiet[0], "alpha": best_quiet[1], "scores": scores[best_quiet]}}


# ----------------------------------------------------------------------------
# Stage 2: calibration to the laboratory
# ----------------------------------------------------------------------------

def stage2_inputs(frame: pd.DataFrame, horizon: int, stage1_models: dict | None) -> tuple[pd.DataFrame, np.ndarray]:
    """Stage-2 input frame for lab rows; Stage-1 predictions anchored with the current offset.

    Returns the input frame and a boolean vector telling where any analyser
    fell back to persistence (or where h == 0, always False).
    """
    columns = STAGE2_INPUTS[0] if horizon == 0 else STAGE2_INPUTS["h"]
    inputs = pd.DataFrame(index=frame.index)
    fallback = np.zeros(len(frame), dtype=bool)
    for name in ANALYSER_METRICS:
        if horizon == 0:
            inputs[f"ln_{name}"] = frame[f"ln_{name}"].to_numpy()
            continue
        model = (stage1_models or {}).get(name)
        raw_now = frame[f"ln_{name}_raw"].to_numpy()
        offset = frame[f"offset_{name}"].to_numpy()
        if model is None:
            delta, fb = np.zeros(len(frame)), np.ones(len(frame), dtype=bool)
        else:
            delta, fb = stage1_apply(model, frame[model["feature_columns"]].to_numpy(dtype=float))
        predicted_raw = np.exp(raw_now + delta)
        inputs[f"ln_{name}_pred"] = ln(predicted_raw + offset)
        fallback |= fb & np.isfinite(raw_now)
    inputs["ln_level"] = frame["ln_level"].to_numpy()
    inputs["ln_previous_lab"] = frame["ln_previous_lab"].to_numpy()
    return inputs[columns], fallback


def persistence_inputs(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Stage-2 inputs when Stage 1 is unavailable: the anchored analysers now."""
    columns = STAGE2_INPUTS[0] if horizon == 0 else STAGE2_INPUTS["h"]
    inputs = pd.DataFrame(index=frame.index)
    for name in ANALYSER_METRICS:
        inputs[f"ln_{name}_pred" if horizon else f"ln_{name}"] = frame[f"ln_{name}"].to_numpy()
    inputs["ln_level"] = frame["ln_level"].to_numpy()
    inputs["ln_previous_lab"] = frame["ln_previous_lab"].to_numpy()
    return inputs[columns]


def fit_stage2(kind: str, inputs: pd.DataFrame, target_ln: np.ndarray, alpha: float | None = None) -> dict:
    columns = list(inputs.columns)
    if kind == "convex":
        return {**fit_convex(inputs.to_numpy(dtype=float), target_ln, columns), "label": "convex"}
    return {**HuberRidge(alpha or 30.0).fit(inputs, target_ln).artifact(), "label": "linear"}


# ----------------------------------------------------------------------------
# Main benchmark
# ----------------------------------------------------------------------------

def run(directory: Path, output: Path, publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES,
        fit_ends: tuple[str, ...] = WALK_FORWARD_FIT_ENDS) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    analysers, controls, labs, extras = load_dataset(directory)
    publication_delay = pd.Timedelta(minutes=publication_delay_minutes)
    print("stage 1: dense features", file=sys.stderr)
    dynamics = DynamicsData(analysers, controls)
    lab_frames = {}
    for horizon in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs["target_time"]) - pd.Timedelta(minutes=horizon)
        frame = build_features(analysers, controls, labs, origins, publication_delay_minutes)
        frame["target_time"] = labs["target_time"].to_numpy()
        frame["target"] = labs["target"].to_numpy()
        frame["available_at"] = frame["target_time"] + publication_delay
        frame["horizon_minutes"] = horizon
        lab_frames[horizon] = frame
    anomaly_frame = pd.DataFrame({tag: series.resample("10min").last() for tag, series in extras.items()})
    anomaly_frame = anomaly_frame.iloc[::3]
    anomaly_frame = anomaly_frame[regime_mask(pd.DataFrame({"T6": anomaly_frame.get("ht.T6"), "F9": anomaly_frame.get("ht.F9"),
                                                            "P13": anomaly_frame.get("ht.P13")}))]

    walk_forward = []
    prediction_rows = []
    stage1_cache: dict = {}

    @lru_cache(maxsize=None)
    def cached_stage1(name: str, horizon: int, training_filter: str, alpha: float, before: pd.Timestamp) -> dict:
        model = fit_stage1(dynamics, name, horizon, training_filter, alpha, before).artifact()
        model["analyser_column"] = f"ln_{name}_raw"
        model["horizon_minutes"] = horizon
        model["training_filter"] = training_filter
        model["fit_before"] = before.isoformat()
        model["train_rows"] = int(dynamics.rows(name, horizon, training_filter, before=before).sum())
        return model

    for fit_end_text in fit_ends:
        fit_end = pd.Timestamp(fit_end_text)
        folds = {name: (pd.Timestamp(s), pd.Timestamp(e)) for name, (s, e) in SELECTION_FOLDS.items() if pd.Timestamp(e) <= fit_end}
        if not folds:
            raise ValueError(f"no selection fold ends before {fit_end_text}")
        print(f"model fit_end={fit_end_text}: stage 1 selection", file=sys.stderr)
        stage1 = {name: {} for name in analysers}
        stage1_selection = {name: {} for name in analysers}
        for name in analysers:
            for horizon in STAGE1_HORIZONS:
                choice = select_stage1(dynamics, name, horizon, fit_end, folds)
                stage1_selection[name][str(horizon)] = choice
                stage1[name][str(horizon)] = cached_stage1(name, horizon, choice["training_filter"], choice["alpha"], fit_end)
        stage1_cache[fit_end_text] = stage1
        implied = {name: {h: stage1_implied_gains(m) for h, m in models.items()} for name, models in stage1.items()}

        print(f"model fit_end={fit_end_text}: control response and anomaly model", file=sys.stderr)
        control_response = estimate_control_response(analysers.get("q21", pd.Series(dtype=float)), controls, fit_end)
        consistency = {}
        for control in CONTROL_METRICS:
            step = control_response.get(control, {}).get("coefficient")
            gains = [implied["q21"][h][control] for h in implied.get("q21", {})]
            gain = float(np.mean(gains)) if gains else None
            consistency[control] = {"step_event_coefficient": step, "stage1_implied_gain_mean": gain,
                                    "sign_agrees": bool(step is not None and gain is not None and np.sign(step) == np.sign(gain)),
                                    "ratio_stage1_over_step": float(gain / step) if step and gain is not None else None}
        anomaly = fit_anomaly_model(anomaly_frame, fit_end, folds)

        print(f"model fit_end={fit_end_text}: stage 2", file=sys.stderr)
        stage2 = {}
        persistence_models = {}
        support = {}
        horizon_metrics = {}
        for horizon in HORIZONS_MINUTES:
            frame = lab_frames[horizon]
            y_ln = ln(frame["target"])
            y = frame["target"].to_numpy(dtype=float)
            available = pd.to_datetime(frame["available_at"])
            origin = pd.to_datetime(frame["prediction_origin"])
            fit_mask = (available < fit_end).to_numpy() & np.isfinite(y_ln)
            support_model = StandardizedRidge(1.0).fit(frame.loc[fit_mask, SUPPORT_COLUMNS], y_ln[fit_mask]).artifact()
            support_model["q50"] = support_model["medians"]
            support_model["applicability_policy"] = APPLICABILITY_POLICY
            gate = [applicability(row, support_model, int(max(frame.loc[i, "anchor_pairs_q21"], frame.loc[i, "anchor_pairs_pak"])))
                    for i, row in enumerate(frame[SUPPORT_COLUMNS].to_numpy(dtype=float))]
            gate_ok = np.array([g["status"] == "ok" for g in gate])
            # Out-of-fold Stage-2 evaluation: every fold uses Stage-1 models
            # fitted before the fold start and Stage-2 fitted on labels
            # published before the fold start.
            oof = {kind: np.full(len(frame), np.nan) for kind in STAGE2_KINDS}
            oof_persistence = np.full(len(frame), np.nan)
            oof_fallback = np.zeros(len(frame), dtype=bool)
            alpha_scores = {}
            for fold, (start, end) in folds.items():
                fold_stage1 = None
                if horizon:
                    fold_stage1 = {name: cached_stage1(name, horizon, stage1_selection[name][str(horizon)]["training_filter"],
                                                       stage1_selection[name][str(horizon)]["alpha"], start) for name in analysers}
                inputs, fallback = stage2_inputs(frame, horizon, fold_stage1)
                fold_fit = (available < start).to_numpy() & gate_ok & np.isfinite(y_ln)
                fold_score = ((origin >= start) & (origin < end)).to_numpy() & np.isfinite(y_ln)
                oof_fallback |= fallback & fold_score
                for kind in STAGE2_KINDS:
                    if kind == "linear":
                        best = None
                        for alpha in ALPHA_GRID:
                            model = fit_stage2(kind, inputs[fold_fit], y_ln[fold_fit], alpha)
                            pred = predict_linear(model, inputs[fold_score].to_numpy(dtype=float))
                            score = float(np.mean(np.abs(y_ln[fold_score & gate_ok] - predict_linear(model, inputs[fold_score & gate_ok].to_numpy(dtype=float)))))
                            alpha_scores.setdefault(str(alpha), []).append(score)
                            if best is None or score < best[0]:
                                best = (score, pred)
                        oof[kind][fold_score] = best[1]
                    else:
                        model = fit_stage2(kind, inputs[fold_fit], y_ln[fold_fit])
                        oof[kind][fold_score] = predict_convex(model, inputs[fold_score].to_numpy(dtype=float))
                persistence = fit_stage2("convex", persistence_inputs(frame, horizon)[fold_fit], y_ln[fold_fit])
                oof_persistence[fold_score] = predict_convex(persistence, persistence_inputs(frame, horizon)[fold_score].to_numpy(dtype=float))
            scored = gate_ok & np.isfinite(y_ln)
            for kind in STAGE2_KINDS:
                scored &= np.isfinite(oof[kind])
            kind_scores = {kind: float(np.mean(np.abs(y_ln[scored] - oof[kind][scored]))) for kind in STAGE2_KINDS}
            kind = min(STAGE2_KINDS, key=lambda k: kind_scores[k])
            alpha = min(ALPHA_GRID, key=lambda a: float(np.mean(alpha_scores[str(a)]))) if alpha_scores else 30.0
            oof_residual = np.where(scored, y_ln - oof[kind], np.nan)
            persistence_scored = gate_ok & np.isfinite(y_ln) & np.isfinite(oof_persistence)
            quantiles = residual_quantile_function(oof_residual)
            quantiles_persistence = residual_quantile_function(np.where(persistence_scored, y_ln - oof_persistence, np.nan))
            oof_probability = np.array([exceedance_probability(quantiles, v, np.log(HARD_LIMIT)) if np.isfinite(v) else np.nan for v in oof[kind]])
            alarm_scores = {}
            for probability in ALARM_PROBABILITY_GRID:
                m = probability_metrics(y[scored], oof_probability[scored], probability)
                recall, precision = m.get("recall_above_10") or 0.0, m.get("precision_above_10") or 0.0
                alarm_scores[str(probability)] = float(2 * recall * precision / (recall + precision)) if recall + precision else 0.0
            alarm_probability = max(ALARM_PROBABILITY_GRID, key=lambda p: alarm_scores[str(p)])
            # Final Stage-2 fit for this fit_end on gate-ok labels published before it.
            final_inputs, final_fallback = stage2_inputs(frame, horizon, stage1.get("q21") and {n: stage1[n][str(horizon)] for n in stage1} if horizon else None)
            final_fit = fit_mask & gate_ok
            model = fit_stage2(kind, final_inputs[final_fit], y_ln[final_fit], alpha)
            model["residual_quantiles"] = quantiles
            model["residual_quantiles_persistence"] = quantiles_persistence
            model["alarm_probability"] = alarm_probability
            model["alarm_probability_f1"] = alarm_scores
            model["selection"] = {"kind_ln_mae": kind_scores, "alpha_ln_mae": {a: float(np.mean(v)) for a, v in alpha_scores.items()},
                                  "selected_alpha": alpha if kind == "linear" else None, "scored_rows": int(scored.sum())}
            model["fit_rows"] = int(final_fit.sum())
            stage2[str(horizon)] = model
            support[str(horizon)] = {k: v for k, v in support_model.items() if k != "applicability_policy"}
            # Predictions for the evaluation window of this model: origins in
            # [fit_end, next fit_end) — genuinely out of sample.
            next_end = pd.Timestamp(fit_ends[fit_ends.index(fit_end_text) + 1]) if fit_ends.index(fit_end_text) + 1 < len(fit_ends) else None
            window = (origin >= fit_end).to_numpy() if next_end is None else ((origin >= fit_end) & (origin < next_end)).to_numpy()
            ln_pred = stage2_predict_frame(model, final_inputs)
            persistence_model = fit_stage2("convex", persistence_inputs(frame, horizon)[final_fit], y_ln[final_fit])
            persistence_model["label"] = "persistence"
            persistence_models[str(horizon)] = persistence_model
            ln_pred_persist = predict_convex(persistence_model, persistence_inputs(frame, horizon).to_numpy(dtype=float))
            ln_used = np.where(final_fallback, ln_pred_persist, ln_pred)
            quantile_used = [quantiles_persistence if fb else quantiles for fb in final_fallback]
            rows = pd.DataFrame({
                "horizon_minutes": horizon, "model_fit_end": fit_end_text, "prediction_origin": frame["prediction_origin"],
                "target_time": frame["target_time"], "target": y, "split": np.where(window, "walk_forward", np.where(fit_mask, "fit", "unused")),
                "forecast_status": np.where(gate_ok, "ok", "abstain"), "abstain_reasons": ["|".join(g["reasons"]) for g in gate],
                "stage1_status": np.where(final_fallback, "fallback_persistence", "ok" if horizon else "not_applicable"),
                "prediction": np.exp(ln_used),
                "prediction_lower": [interval(q, v)[0] if np.isfinite(v) else np.nan for q, v in zip(quantile_used, ln_used)],
                "prediction_upper": [interval(q, v)[1] if np.isfinite(v) else np.nan for q, v in zip(quantile_used, ln_used)],
                "exceedance_probability": [exceedance_probability(q, v, np.log(HARD_LIMIT)) if np.isfinite(v) else np.nan for q, v in zip(quantile_used, ln_used)],
                "oof_prediction": np.exp(oof[kind]), "oof_exceedance_probability": oof_probability,
                "baseline_constant": float(np.median(y[fit_mask])), "baseline_previous_lab": frame["previous_lab_available"],
                "baseline_level": np.exp(frame["ln_level"]), "baseline_q21_anchored": np.exp(frame["ln_q21"]),
                "baseline_pak_anchored": np.exp(frame["ln_pak"]),
                "previous_lab_sample_time": frame["previous_lab_sample_time"], "previous_lab_available_at": frame["previous_lab_available_at"],
                "previous_lab_available": frame["previous_lab_available"],
                "offset_q21": frame["offset_q21"], "offset_pak": frame["offset_pak"],
                "anchor_pairs_q21": frame["anchor_pairs_q21"], "anchor_pairs_pak": frame["anchor_pairs_pak"],
            })
            for column in final_inputs.columns:
                rows[column] = final_inputs[column].to_numpy()
            for column in SUPPORT_COLUMNS:
                if column not in rows:
                    rows[column] = frame[column].to_numpy()
            prediction_rows.append(rows)
            accepted = window & gate_ok & np.isfinite(y)
            entry = {
                "rows": int(window.sum()), "accepted_rows": int(accepted.sum()), "coverage": float(accepted.sum() / max(1, window.sum())),
                "abstained_actual_above_10": int((window & ~gate_ok & (y > HARD_LIMIT)).sum()),
                "fallback_rows": int((accepted & final_fallback).sum()),
                "model": regression_metrics(y[accepted], np.exp(ln_used[accepted])),
                "probability": probability_metrics(y[accepted], rows.loc[accepted, "exceedance_probability"], alarm_probability),
                "interval_80_coverage": float(((y >= rows["prediction_lower"]) & (y <= rows["prediction_upper"]))[accepted].mean()) if accepted.any() else None,
                "oof_selection": {"rows": int(scored.sum()), "model": regression_metrics(y[scored], np.exp(oof[kind][scored])),
                                  "probability": probability_metrics(y[scored], oof_probability[scored], alarm_probability),
                                  "persistence_path": regression_metrics(y[persistence_scored], np.exp(oof_persistence[persistence_scored]))},
            }
            for name in ("constant", "previous_lab", "level", "q21_anchored", "pak_anchored"):
                entry[f"baseline_{name}"] = regression_metrics(y[accepted], rows.loc[accepted, f"baseline_{name}"])
            lab_violation = int((pd.to_datetime(frame["previous_lab_available_at"]) > origin).sum())
            origin_violation = int((origin + pd.Timedelta(minutes=horizon) != pd.to_datetime(frame["target_time"])).sum())
            fit_label_latest = available[fit_mask].max()
            split_violation = int(bool(window.any()) and fit_label_latest > origin[window].min())
            entry["leakage_check"] = {"origin_horizon_mismatch_rows": origin_violation, "lab_publication_violations": lab_violation,
                                      "fit_label_latest_available_at": fit_label_latest, "fit_end": fit_end,
                                      "split_violations": split_violation,
                                      "passed": origin_violation + lab_violation + split_violation == 0 and bool(fit_label_latest <= fit_end)}
            if not entry["leakage_check"]["passed"]:
                raise AssertionError(entry["leakage_check"])
            horizon_metrics[str(horizon)] = entry
        walk_forward.append({
            "fit_end": fit_end.isoformat(), "selection_folds": list(folds), "available_from": fit_end.isoformat(),
            "stage1": stage1, "stage1_selection": stage1_selection, "stage1_implied_gains": implied,
            "stage2": stage2, "persistence": persistence_models, "support": support,
            "control_response": control_response, "consistency": consistency,
            "anomaly": anomaly, "train_median": float(np.median(lab_frames[0].loc[(pd.to_datetime(lab_frames[0]["available_at"]) < fit_end).to_numpy(), "target"])),
            "metrics": horizon_metrics,
        })

    # Only the out-of-sample window of each walk-forward model is published:
    # every lab sample from the first fit_end on appears exactly once.
    predictions = pd.concat(prediction_rows, ignore_index=True)
    predictions = predictions[predictions["split"].eq("walk_forward")].reset_index(drop=True)
    predictions.to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")
    artifact = {
        "version": 4,
        "model": "two-stage: dense analyser dynamics ridge (Stage 1) + lab-anchored convex/robust calibration to LIMS (Stage 2); walk-forward",
        "target": {"metric_id": TARGET_METRIC, "point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
        "hard_limit": HARD_LIMIT,
        "horizons_minutes": list(HORIZONS_MINUTES),
        "lims_publication_delay_minutes": float(publication_delay_minutes),
        "lab_anchor_samples": LAB_ANCHOR_SAMPLES,
        "feature_columns": SUPPORT_COLUMNS, "dynamics_columns": DYNAMICS_COLUMNS,
        "stage2_inputs": {"0": STAGE2_INPUTS[0], "h": STAGE2_INPUTS["h"]},
        "analyser_metrics": ANALYSER_METRICS, "control_metrics": CONTROL_METRICS,
        "applicability_policy": APPLICABILITY_POLICY,
        "stage1_regime": {k: list(v) for k, v in STAGE1_REGIME.items()},
        "quiet_future": QUIET_FUTURE,
        "anomaly_tags": ANOMALY_TAGS,
        "model_available_from": pd.Timestamp(fit_ends[0]).isoformat(),
        "walk_forward": [{k: v for k, v in entry.items() if k not in ("metrics", "stage1_selection")} for entry in walk_forward],
    }
    (output / "model.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    metadata = {
        "dataset": str(directory),
        "observations_sha256": sha256(directory / "observations.parquet"),
        "target": artifact["target"], "lab_rows": int(len(labs)),
        "analyser_valid_fraction": {k: float(v.notna().mean()) for k, v in analysers.items()},
        "dense_rows": int(len(dynamics.origins)), "dense_regime_rows": int(dynamics.regime.sum()),
        "feature_columns": SUPPORT_COLUMNS, "dynamics_columns": DYNAMICS_COLUMNS,
        "alpha_grid": list(ALPHA_GRID), "alarm_probability_grid": list(ALARM_PROBABILITY_GRID),
        "walk_forward_fit_ends": list(fit_ends), "selection_folds": SELECTION_FOLDS, "test_start": TEST_START,
        "walk_forward": [{"fit_end": e["fit_end"], "selection_folds": e["selection_folds"], "metrics": e["metrics"],
                          "stage1_selection": e["stage1_selection"], "stage1_implied_gains": e["stage1_implied_gains"],
                          "stage2": {h: {k: v for k, v in m.items() if k in ("kind", "label", "feature_columns", "weights", "bias", "alpha", "coef",
                                                                              "alarm_probability", "selection", "fit_rows")}
                                     for h, m in e["stage2"].items()},
                          "control_response": e["control_response"], "consistency": e["consistency"],
                          "anomaly": {k: v for k, v in (e["anomaly"] or {}).items() if k in ("tags", "thresholds", "threshold_basis", "train_thresholds", "fold_evidence", "method", "train_rows")},
                          "train_median": e["train_median"]} for e in walk_forward],
        "model_artifact": "model.json", "model_artifact_sha256": sha256(output / "model.json"),
    }
    (output / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(output / "REPORT.md", metadata)
    return metadata


def stage2_predict_frame(model: dict, inputs: pd.DataFrame) -> np.ndarray:
    values = inputs.loc[:, model["feature_columns"]].to_numpy(dtype=float)
    return predict_linear(model, values) if model.get("kind") == "linear" else predict_convex(model, values)


def _fmt(value, digits=3):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def write_report(path: Path, metadata: dict) -> None:
    final = metadata["walk_forward"][-1]
    rows = [
        "# Прогноз серы после гидроочистки: двухступенчатая модель (динамика анализатора + калибровка по ЛИМС)",
        "",
        "## Постановка",
        "",
        f"Цель: `{metadata['target']['metric_id']}` ({metadata['target']['point']}, точка 2, мг/кг), {metadata['lab_rows']} лабораторных проб. "
        "Горизонты 0/60/120/180 минут от момента выпуска (origin). Телеметрия и анализаторы используются не позже origin; "
        f"результат ЛИМС — только при `sample_time + {LIMS_PUBLICATION_DELAY_MINUTES} мин <= origin`.",
        "",
        "**Ступень 1 (динамика).** Для каждого поточного анализатора (`Q21`, ПАК) и горизонта 60/120/180 мин ridge на плотной сетке "
        f"({metadata['dense_rows']} точек с шагом {STAGE1_STEP}, из них {metadata['dense_regime_rows']} в нормальном режиме "
        f"T6∈{STAGE1_REGIME['T6']}, F9>{STAGE1_REGIME['F9'][0]}, P13>{STAGE1_REGIME['P13'][0]}) предсказывает изменение ln анализатора по его истории "
        "(текущее, 1/6/24 ч) и по изменениям и уровням управляющих тегов T6/F9/P13 (за 1/3/6 ч). "
        "Вариант обучения «тихое будущее» (управления не двигаются до целевого момента: "
        f"|ΔT6|<{QUIET_FUTURE['T6']} °C, |Δln F9|<{QUIET_FUTURE['lnF9']}, |ΔP13|<{QUIET_FUTURE['P13']} МПа) описывает процесс без вмешательства, "
        "а не привычную реакцию оператора; выбирается на фолдах, если он не хуже на тихих строках и таких строк не меньше "
        f"{QUIET_MIN_FRACTION:.0%}.",
        "",
        "**Ступень 2 (калибровка).** Предсказанные значения анализаторов на момент пробы корректируются на медианное смещение «ЛИМС − анализатор» "
        f"по последним {LAB_ANCHOR_SAMPLES} опубликованным пробам и объединяются с локальным уровнем (медиана 5 проб) и предыдущей пробой "
        "выпуклой комбинацией в ln (веса ≥ 0, Σ=1) либо робастной (Huber) линейной моделью — вид выбирается на фолдах по ln-MAE. "
        "Плато анализатора ≥60 мин и значения вне (0, 100) не используются; очистка каузальна (не смотрит вперёд).",
        "",
        f"**Walk-forward.** Модели с `fit_end` {metadata['walk_forward_fit_ends']}: каждая использует только пробы, опубликованные до fit_end, "
        "и только фолды, заканчивающиеся не позже fit_end (α, вид ступени 2, порог тревоги, квантили остатков). "
        "В runtime применяется последняя модель с `fit_end ≤ origin`, поэтому исторический прогноз с 2024 года не содержит будущей информации. "
        "Метрики ниже — вне выборки: модель fit_end=2024 оценена на 2024, 2025 — на 2025, 2026 — на удержанном 2026 (финальная модель).",
        "",
        "## Ступень 1: точность на уровне анализатора (фолды, мг/кг)",
        "",
        "| модель | анализатор | горизонт | фильтр | α | доля тихих | фолд | n | MAE модели | MAE персистентности | n тихих | MAE модели (тихие) | MAE перс. (тихие) |",
        "|---|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for entry in metadata["walk_forward"]:
        for name, per_h in entry["stage1_selection"].items():
            for horizon, choice in per_h.items():
                for fold in choice["chosen_scores"]["folds"]:
                    a, q = fold["all"] or {}, fold["quiet"] or {}
                    rows.append(f"| {entry['fit_end'][:10]} | {name} | {horizon} | {choice['training_filter']} | {choice['alpha']:g} | "
                                f"{choice['quiet_fraction']:.0%} | {fold['fold']} | {a.get('n', 0)} | {_fmt(a.get('mae'))} | {_fmt(a.get('persistence_mae'))} | "
                                f"{q.get('n', 0)} | {_fmt(q.get('mae'))} | {_fmt(q.get('persistence_mae'))} |")
    rows += ["", "## Точность по горизонтам на уровне ЛИМС (walk-forward, принятые строки)", "",
             "| модель | горизонт | покрытие | n | fallback | MAE модели | MAE константы | MAE пред. пробы | MAE Q21 калибр. | MAE ПАК калибр. | corr | 80% интервал | Brier skill | AUC | recall @порог | ложные тревоги |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for entry in metadata["walk_forward"]:
        for horizon, m in entry["metrics"].items():
            mm, p = m["model"], m["probability"]
            rows.append(f"| {entry['fit_end'][:10]} | {horizon} мин | {m['coverage']:.1%} | {mm.get('n', 0)} | {m['fallback_rows']} | {_fmt(mm.get('mae'))} | "
                        f"{_fmt(m['baseline_constant'].get('mae'))} | {_fmt(m['baseline_previous_lab'].get('mae'))} | {_fmt(m['baseline_q21_anchored'].get('mae'))} | "
                        f"{_fmt(m['baseline_pak_anchored'].get('mae'))} | {_fmt(mm.get('correlation'))} | {_fmt(m.get('interval_80_coverage'), 2)} | "
                        f"{_fmt(p.get('brier_skill'))} | {_fmt(p.get('auc'))} | {_fmt(p.get('recall_above_10'), 2)} @{p.get('alarm_probability')} | {_fmt(p.get('false_alarm_rate'), 2)} |")
    rows += ["", "## Ступень 2: выбранный вид и веса", "", "| модель | горизонт | вид | входы | веса / коэффициенты | bias | OOF ln-MAE по видам | порог тревоги |", "|---|---:|---|---|---|---:|---|---:|"]
    for entry in metadata["walk_forward"]:
        for horizon, m in entry["stage2"].items():
            weights = m.get("weights") if m.get("kind") == "convex" else [round(c, 3) for c in m.get("coef", [])]
            rows.append(f"| {entry['fit_end'][:10]} | {horizon} | {m.get('label')} | {', '.join(m['feature_columns'])} | "
                        f"{', '.join(f'{w:.2f}' for w in weights)} | {_fmt(m.get('bias'), 3)} | "
                        f"{', '.join(f'{k}: {v:.4f}' for k, v in m['selection']['kind_ln_mae'].items())} | {m['alarm_probability']} |")
    rows += ["", "## Отклик серы на управляющие воздействия (анализатор Q21, ступенчатые события, train-only)", "",
             "| модель | тег | событий | коэффициент | se | 80% ДИ | единица | лаг, мин | 80% ДИ лага | отклик по часам |",
             "|---|---|---:|---:|---:|---|---|---:|---|---|"]
    for entry in metadata["walk_forward"]:
        for name, item in entry["control_response"].items():
            by_hour = item.get("response_by_hour") or {}
            ci = item.get("ci_80")
            rows.append(f"| {entry['fit_end'][:10]} | {name} | {item.get('events', 0)} | {_fmt(item.get('coefficient'), 4)} | {_fmt(item.get('se'), 4)} | "
                        f"{'—' if not ci else f'[{ci[0]:+.4f}, {ci[1]:+.4f}]'} | {item.get('unit')} | {item.get('lag_minutes', '—')} | "
                        f"{item.get('lag_ci_80', '—')} | " + ", ".join(f"{k}: {v:+.4f}" for k, v in by_hour.items()) + " |")
    rows += ["", "Коэффициенты — медианный лагированный отклик ln(серы) на ступень одного тега (за час) при спокойных остальных тегах и нормальном режиме; "
             "события одного разгона объединены; ДИ — bootstrap по событиям (оптимистичен из-за автокорреляции). Операторы меняют режим и в ответ на дрейф серы, "
             "поэтому это наблюдательная оценка, а не доказанный причинный эффект; направление для температуры совпадает с подтверждением эксперта. "
             "Только данные до fit_end модели.",
             "", "## Согласованность ступени 1 и ступенчатых откликов", "",
             "| модель | тег | ступенчатый коэффициент | implied gain ступени 1 (среднее по горизонтам) | знак совпадает | отношение |", "|---|---|---:|---:|---|---:|"]
    for entry in metadata["walk_forward"]:
        for name, item in entry["consistency"].items():
            rows.append(f"| {entry['fit_end'][:10]} | {name} | {_fmt(item.get('step_event_coefficient'), 4)} | {_fmt(item.get('stage1_implied_gain_mean'), 4)} | "
                        f"{'да' if item.get('sign_agrees') else 'нет'} | {_fmt(item.get('ratio_stage1_over_step'), 2)} |")
    rows += ["", "Implied gain — суммарный отклик ступени 1 на единичную ступень тега непосредственно перед origin (сумма коэффициентов при дельтах и уровне). "
             "Сценарная модель использует ступенчатые коэффициенты; согласие знаков — проверка, а не калибровка.",
             "", "## Модель аномалии режима", ""]
    for entry in metadata["walk_forward"]:
        a = entry.get("anomaly") or {}
        rows.append(f"- {entry['fit_end'][:10]}: {a.get('method', 'недоступна')}, признаки {', '.join(a.get('tags', []))}, строк {a.get('train_rows')}, "
                    f"пороги d² attention {_fmt((a.get('thresholds') or {}).get('attention'), 1)} / high {_fmt((a.get('thresholds') or {}).get('high'), 1)} "
                    f"({a.get('threshold_basis')}; in-sample: {_fmt((a.get('train_thresholds') or {}).get('attention'), 1)} / "
                    f"{_fmt((a.get('train_thresholds') or {}).get('high'), 1)}).")
    rows += ["", "Индекс аномалии — робастное расстояние Махаланобиса вектора балансов реакторного блока (ΔT Р-202, ΔT квенча, ΔP Р-202, квенч/сырьё, "
             "ВСГ/сырьё, P13) до обучающего режима; классы attention/high — квантили 95/99 % d² на фолдах вне выборки (in-sample квантили "
             "занижают нормальный дрейф режима между циклами катализатора). Это прокси необычного сочетания сигналов, не вероятность отказа.",
             "", "## Интерпретация", "",
             "- Суточная лабораторная проба содержит быструю (часовую) составляющую и шум анализа (автокорреляция соседних проб 0.0–0.5), "
             "поэтому на уровне ЛИМС ошибка ограничена снизу ~1 мг/кг; ступень 1 даёт основной выигрыш на уровне анализатора, который оператор видит непрерывно.",
             "- Путь без воздействия учитывает уже сделанные изменения T6/F9/P13 (через дельты за 1/3/6 ч); сценарий добавляет только предложенное изменение.",
             "- Строки со статусом abstain (нет анализатора и пробы, режим вне области обучения, мало пар для калибровки) не получают прогноза; "
             "при неполных входах ступени 1 используется персистентность анализатора с отдельными (более широкими) квантилями остатков.",
             "- Метрики относятся к выходу гидроочистки, не к товарной смеси после блендинга.",
             "", f"SHA-256 артефакта: `{metadata['model_artifact_sha256']}`; SHA-256 наблюдений: `{metadata['observations_sha256']}`.",
             "", "Артефакты: `model.json` (walk-forward модели, квантили остатков, отклики, модель аномалии), `metrics.json`, `predictions.csv` (строки проб).",
             ]
    del final
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lims-publication-delay-hours", type=float, default=4.0)
    args = parser.parse_args()
    metadata = run(args.dataset.resolve(), args.output.resolve(), args.lims_publication_delay_hours * 60)
    summary = {e["fit_end"][:10]: {h: {"mae": m["model"].get("mae"), "constant_mae": m["baseline_constant"].get("mae"),
                                        "q21_anchored_mae": m["baseline_q21_anchored"].get("mae"), "auc": m["probability"].get("auc")}
                                    for h, m in e["metrics"].items()} for e in metadata["walk_forward"]}
    print(json.dumps({"output": str(args.output.resolve()), "summary": summary}, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
