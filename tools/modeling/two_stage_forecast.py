"""Repaired two-stage sulphur nowcast/forecast ("v4r", artifact version 5).

Source: the v4 candidate ``feat/forecast-v4-contour-v2@2397769`` (Stage 1:
dense analyser-dynamics ridge per analyser/horizon; Stage 2: lab-anchored
convex/Huber calibration to LIMS).  The audit of that candidate found
temporal defects; this module repairs them without changing the model family
(protocol: ``reports/review/v4-repair-2026-09-22/PROTOCOL.md``):

1. a Stage-1 row belongs to a fold (or a training prefix) only if its whole
   target window ``(origin+h-30, origin+h+30]`` ends before the boundary;
2. the ``quiet_future`` training filter checks the whole control path, not
   only the endpoints (``two_stage_features.future_path_quiet``);
3. Stage-2 configuration (kind, alpha) and Stage-1 configuration for an outer
   fold are chosen only on earlier folds (nested, recursive); the first fold
   uses a preregistered configuration; residual quantiles come only from those
   outer predictions and the alarm probability is fixed (0.30);
4. the applicability (support) gate of an outer fold is fitted on labels
   published before the fold start;
5. a fold row is scored only when its label is published before the fold end.

Walk-forward artifacts: one model set per ``fit_end``; the runtime uses the
latest set whose ``fit_end`` is not after the origin.  2024 and 2025 are the
development years of the frozen protocol; 2026 was examined in earlier work
and is a retrospective audit, not a blind holdout.

Example::

    python tools/modeling/two_stage_forecast.py --dataset storage/hackathon \
        --output reports/modeling/two-stage-v4r
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.modeling.two_stage_features import (  # noqa: E402
    ANALYSER_METRICS,
    ANALYSER_TARGET_HALF_WINDOW_MINUTES,
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
    analyser_target,
    anomaly_vector,
    applicability,
    build_dynamics_features,
    build_features,
    clean_analyser,
    exceedance_probability,
    future_path_quiet,
    interval,
    ln,
    predict_convex,
    predict_linear,
    regime_mask,
    residual_quantile_function,
)

ARTIFACT_VERSION = 5
TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
TARGET_POINT = "Гидроочистка"
TARGET_PARAMETER = "Mg.Sulfur"
HARD_LIMIT = 10.0
ALPHA_GRID = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0)
# Fixed before evaluation (protocol item 3), as in the v3 protocol; not optimised.
ALARM_PROBABILITY = 0.30
WALK_FORWARD_FIT_ENDS = ("2024-01-01", "2025-01-01", "2026-01-01")
SELECTION_FOLDS = {"2023H2": ("2023-07-01", "2024-01-01"), "2024": ("2024-01-01", "2025-01-01"),
                   "2025": ("2025-01-01", "2026-01-01")}
DEVELOPMENT_YEARS = (2024, 2025)
AUDIT_START = "2026-01-01"
STAGE1_STEP = "30min"
STAGE1_HORIZONS = tuple(h for h in HORIZONS_MINUTES if h > 0)
# "Quiet future" training filter: the controls stay within these limits of
# their origin value over the whole target window (see future_path_quiet).
QUIET_FUTURE = {"T6": 1.0, "lnF9": 0.02, "P13": 0.05}
QUIET_MIN_FRACTION = 0.10
QUIET_TOLERANCE = 1.05
# Configurations used when no earlier fold exists (the first outer fold).
PREREGISTERED_STAGE1 = {"training_filter": "all", "alpha": 30.0}
PREREGISTERED_STAGE2 = {"kind": "convex", "alpha": 30.0}
MIN_SUPPORT_ROWS = 30
BOOTSTRAP = 200
SEED = 0
CONVEX_STEP = 0.05
STAGE2_CONFIGS = (("convex", None), *(("linear", alpha) for alpha in ALPHA_GRID))


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


def load_dataset(directory: Path) -> tuple[dict[str, pd.Series], dict[str, pd.Series], pd.DataFrame, dict[str, pd.Series],
                                          dict[str, pd.Series]]:
    """Cleaned analysers, control telemetry, the lab target, anomaly tags and control ``flatline`` masks.

    Controls keep their flatline-flagged readings as features (the runtime
    does the same); the masks only exclude them from the quiet-path check.
    """
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
    controls, control_invalid = {}, {}
    for name, mid in CONTROL_METRICS.items():
        part = groups.get(mid)
        if part is not None:
            controls[name] = part["value"].astype(float)
            control_invalid[name] = part["flags"].astype(str).str.contains("flatline")
    lab = groups.get(TARGET_METRIC)
    if lab is None:
        raise ValueError(f"{TARGET_METRIC} is missing from the dataset")
    labs = pd.DataFrame({"target_time": lab.index, "target": lab["value"].to_numpy(dtype=float)})
    labs = labs[labs["target"] >= 0].reset_index(drop=True)
    extras = {mid: groups[mid]["value"].astype(float) for mid in ANOMALY_TAGS if mid in groups}
    return analysers, controls, labs, extras, control_invalid


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
    """Dense-grid Stage-1 frame with targets per analyser/horizon and whole-path quiet masks.

    ``window_end[h]`` is the end of the target window of each origin
    (``origin + h + 30 min``); every boundary check uses it, never the origin.
    ``control_invalid`` maps a control to a boolean Series of readings that
    must not count as observed for the quiet check (importer ``flatline``).
    """

    def __init__(self, analysers: dict[str, pd.Series], controls: dict[str, pd.Series],
                 control_invalid: dict[str, pd.Series] | None = None):
        self.analysers = analysers
        self.controls = controls
        starts = [s.index.min() for s in list(analysers.values()) + list(controls.values()) if len(s)]
        ends = [s.index.max() for s in list(analysers.values()) + list(controls.values()) if len(s)]
        self.origins = pd.date_range(min(starts).ceil("h") + pd.Timedelta(hours=24), max(ends), freq=STAGE1_STEP)
        self.frame = build_dynamics_features(analysers, controls, self.origins)
        self.regime = regime_mask(self.frame)
        self.targets: dict[tuple[str, int], np.ndarray] = {}
        self.quiet: dict[int, np.ndarray] = {}
        self.window_end = {h: self.origins + pd.Timedelta(minutes=h + ANALYSER_TARGET_HALF_WINDOW_MINUTES)
                           for h in STAGE1_HORIZONS}
        origin_ns = self.origins.as_unit("ns").asi8
        paths = {}
        for control in ("T6", "F9", "P13"):
            series = controls.get(control)
            if series is not None and control_invalid and control in control_invalid:
                bad = control_invalid[control].reindex(series.index, fill_value=False).to_numpy(dtype=bool)
                series = series[~bad]
            paths[control] = series
        for horizon in STAGE1_HORIZONS:
            for name, series in analysers.items():
                self.targets[(name, horizon)] = ln(analyser_target(series, self.origins, horizon)) - self.frame[f"ln_{name}_raw"].to_numpy()
            end_ns = self.window_end[horizon].as_unit("ns").asi8
            quiet = np.ones(len(self.origins), dtype=bool)
            for control, key, log in (("T6", "T6", False), ("F9", "lnF9", True), ("P13", "P13", False)):
                if paths[control] is None:
                    quiet &= False
                    continue
                quiet &= future_path_quiet(paths[control], origin_ns, end_ns, QUIET_FUTURE[key], log=log)
            self.quiet[horizon] = quiet

    def base_rows(self, name: str, horizon: int, training_filter: str = "all") -> np.ndarray:
        mask = self.regime & np.isfinite(self.targets[(name, horizon)]) & np.isfinite(self.frame[f"ln_{name}_raw"].to_numpy())
        if training_filter == "quiet_future":
            mask &= self.quiet[horizon]
        return mask

    def rows(self, name: str, horizon: int, training_filter: str, before: pd.Timestamp | None = None,
             between: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> np.ndarray:
        mask = self.base_rows(name, horizon, training_filter)
        end = self.window_end[horizon]
        if before is not None:
            mask &= np.asarray(end < pd.Timestamp(before))
        if between is not None:
            start, stop = pd.Timestamp(between[0]), pd.Timestamp(between[1])
            mask &= np.asarray((self.origins >= start) & (self.origins < stop) & (end < stop))
        return mask


def crossing_report(data: DynamicsData, folds: dict) -> list[dict]:
    """Rows an origin-only fold filter would keep although their target window crosses the fold end."""
    report = []
    for fold, (start, end) in folds.items():
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        for horizon in STAGE1_HORIZONS:
            for name in data.analysers:
                origin_only = data.base_rows(name, horizon) & np.asarray((data.origins >= start) & (data.origins < end))
                kept = data.rows(name, horizon, "all", between=(start, end))
                report.append({"fold": fold, "horizon": horizon, "analyser": name,
                               "origin_only_rows": int(origin_only.sum()), "kept_rows": int(kept.sum()),
                               "removed_rows": int((origin_only & ~kept).sum())})
    return report


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
    """Δln for rows ordered as ``model["feature_columns"]``; persistence (0) where inputs are too sparse."""
    raw = np.atleast_2d(np.asarray(raw, dtype=float))
    delta = predict_linear(model, raw)
    own = model["feature_columns"].index(model["analyser_column"])
    missing = (~np.isfinite(raw)).mean(axis=1)
    fallback = ~np.isfinite(raw[:, own]) | (missing > policy["max_missing_fraction"]) | ~np.isfinite(delta)
    delta = np.where(fallback, 0.0, delta)
    return delta, fallback


def select_stage1(data: DynamicsData, name: str, horizon: int, cutoff: pd.Timestamp, folds: dict,
                  cache: dict | None = None) -> dict:
    """Choose training filter and alpha on ``folds`` (all ending at or before ``cutoff``).

    Scoring rows lie wholly inside their fold (target window included).
    Without any fold the preregistered configuration is returned.
    """
    if not folds:
        return {**PREREGISTERED_STAGE1, "basis": "preregistered", "quiet_fraction": None}
    cache = {} if cache is None else cache
    scores = {}
    all_rows = max(1, int(data.rows(name, horizon, "all", before=cutoff).sum()))
    fraction = float(data.rows(name, horizon, "quiet_future", before=cutoff).sum() / all_rows)
    for training_filter in ("all", "quiet_future"):
        for alpha in ALPHA_GRID:
            per_fold = []
            for fold, (start, end) in folds.items():
                key = (name, horizon, training_filter, alpha, pd.Timestamp(start))
                if key not in cache:
                    cache[key] = fit_stage1(data, name, horizon, training_filter, alpha, pd.Timestamp(start))
                model = cache[key]
                entry = {"fold": fold}
                for label, flt in (("all", "all"), ("quiet", "quiet_future")):
                    mask = data.rows(name, horizon, flt, between=(start, end))
                    if not mask.any():
                        entry[label] = None
                        continue
                    base = data.frame.loc[mask, f"ln_{name}_raw"].to_numpy()
                    actual = np.exp(base + data.targets[(name, horizon)][mask])
                    pred = np.exp(base + model.predict(data.frame.loc[mask]))
                    entry[label] = {"n": int(mask.sum()), "mae": float(np.mean(np.abs(actual - pred))),
                                    "persistence_mae": float(np.mean(np.abs(actual - np.exp(base))))}
                per_fold.append(entry)
            scores[(training_filter, alpha)] = {
                "folds": per_fold, "quiet_fraction": fraction,
                "mean_mae_all": float(np.mean([f["all"]["mae"] for f in per_fold if f["all"]])),
                "mean_mae_quiet": float(np.mean([f["quiet"]["mae"] for f in per_fold if f["quiet"]]))}
    best_all = min((k for k in scores if k[0] == "all"), key=lambda k: scores[k]["mean_mae_all"])
    best_quiet = min((k for k in scores if k[0] == "quiet_future"), key=lambda k: scores[k]["mean_mae_quiet"])
    # The quiet-future model describes "no further intervention" but is trained
    # on a selected sample: used only when better on quiet rows and not
    # materially worse overall (rule inherited unchanged from v4).
    quiet_ok = (fraction >= QUIET_MIN_FRACTION
                and scores[best_quiet]["mean_mae_quiet"] <= scores[best_all]["mean_mae_quiet"]
                and scores[best_quiet]["mean_mae_all"] <= QUIET_TOLERANCE * scores[best_all]["mean_mae_all"])
    chosen = best_quiet if quiet_ok else best_all
    other = best_all if quiet_ok else best_quiet
    return {"training_filter": chosen[0], "alpha": chosen[1], "basis": "earlier_folds", "folds": list(folds),
            "quiet_fraction": fraction, "chosen_scores": scores[chosen],
            "alternative": {"training_filter": other[0], "alpha": other[1], "scores": scores[other]},
            "grid": {f"{k[0]}/{k[1]:g}": {"mean_mae_all": v["mean_mae_all"], "mean_mae_quiet": v["mean_mae_quiet"]}
                     for k, v in scores.items()}}


# ----------------------------------------------------------------------------
# Stage 2: calibration to the laboratory
# ----------------------------------------------------------------------------

def stage2_inputs(frame: pd.DataFrame, horizon: int, stage1_models: dict | None) -> tuple[pd.DataFrame, np.ndarray]:
    """Stage-2 inputs for lab rows (Stage-1 predictions anchored with the current offset) and fallback flags."""
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
        inputs[f"ln_{name}_pred"] = ln(np.exp(raw_now + delta) + offset)
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
    return {**HuberRidge(alpha or PREREGISTERED_STAGE2["alpha"]).fit(inputs, target_ln).artifact(), "label": "linear"}


def stage2_predict_frame(model: dict, inputs: pd.DataFrame) -> np.ndarray:
    values = inputs.loc[:, model["feature_columns"]].to_numpy(dtype=float)
    return predict_linear(model, values) if model.get("kind") == "linear" else predict_convex(model, values)


# ----------------------------------------------------------------------------
# Nested procedure: one model set per cutoff, configured on earlier folds only
# ----------------------------------------------------------------------------

class Context:
    """Shared, past-only feature frames and caches for fitting model sets."""

    def __init__(self, dynamics: DynamicsData, lab_frames: dict[int, pd.DataFrame], publication_delay_minutes: float):
        self.dynamics = dynamics
        self.lab_frames = lab_frames
        self.publication_delay_minutes = float(publication_delay_minutes)
        self.y_ln = {h: ln(frame["target"]) for h, frame in lab_frames.items()}
        self.available = {h: pd.to_datetime(frame["available_at"]) for h, frame in lab_frames.items()}
        self.origin = {h: pd.to_datetime(frame["prediction_origin"]) for h, frame in lab_frames.items()}
        self.stage1_cache: dict = {}
        self.stage1_artifacts: dict = {}
        self.procedures: dict = {}

    def fit_rows(self, horizon: int, cutoff) -> np.ndarray:
        """Labels published before ``cutoff``."""
        return (self.available[horizon] < pd.Timestamp(cutoff)).to_numpy() & np.isfinite(self.y_ln[horizon])

    def fold_rows(self, horizon: int, start, end) -> np.ndarray:
        """Origins in ``[start, end)`` whose label is published before ``end``."""
        return ((self.origin[horizon] >= pd.Timestamp(start)) & (self.available[horizon] < pd.Timestamp(end))).to_numpy() \
            & np.isfinite(self.y_ln[horizon])

    def stage1_model(self, name: str, horizon: int, training_filter: str, alpha: float, before) -> dict:
        key = (name, horizon, training_filter, float(alpha), pd.Timestamp(before))
        if key not in self.stage1_artifacts:
            if key not in self.stage1_cache:
                self.stage1_cache[key] = fit_stage1(self.dynamics, name, horizon, training_filter, alpha, pd.Timestamp(before))
            model = self.stage1_cache[key].artifact()
            model.update({"analyser_column": f"ln_{name}_raw", "horizon_minutes": horizon, "training_filter": training_filter,
                          "fit_before": pd.Timestamp(before).isoformat(),
                          "train_rows": int(self.dynamics.rows(name, horizon, training_filter, before=before).sum())})
            self.stage1_artifacts[key] = model
        return self.stage1_artifacts[key]


def build_context(analysers: dict[str, pd.Series], controls: dict[str, pd.Series], labs: pd.DataFrame,
                  publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES,
                  control_invalid: dict[str, pd.Series] | None = None) -> Context:
    dynamics = DynamicsData(analysers, controls, control_invalid)
    delay = pd.Timedelta(minutes=publication_delay_minutes)
    lab_frames = {}
    for horizon in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs["target_time"]) - pd.Timedelta(minutes=horizon)
        frame = build_features(analysers, controls, labs, origins, publication_delay_minutes)
        frame["target_time"] = pd.DatetimeIndex(labs["target_time"]).to_numpy()
        frame["target"] = labs["target"].to_numpy(dtype=float)
        frame["available_at"] = frame["target_time"] + delay
        frame["horizon_minutes"] = horizon
        lab_frames[horizon] = frame.reset_index(drop=True)
    return Context(dynamics, lab_frames, publication_delay_minutes)


def fit_support(ctx: Context, horizon: int, cutoff) -> dict:
    """Applicability envelope from labels published before ``cutoff`` only."""
    rows = ctx.fit_rows(horizon, cutoff)
    if rows.sum() < MIN_SUPPORT_ROWS:
        raise ValueError(f"only {int(rows.sum())} labels before {cutoff} for the support gate")
    frame = ctx.lab_frames[horizon]
    model = StandardizedRidge(1.0).fit(frame.loc[rows, SUPPORT_COLUMNS], ctx.y_ln[horizon][rows]).artifact()
    model["q50"] = model["medians"]
    model["fit_before"] = pd.Timestamp(cutoff).isoformat()
    model["fit_rows"] = int(rows.sum())
    return model


def _gates(ctx: Context, model_set: dict, horizon: int) -> list[dict]:
    """Applicability of every lab row of ``horizon`` under the model set's own support (cached)."""
    cache = model_set.setdefault("_gates", {})
    if horizon not in cache:
        frame = ctx.lab_frames[horizon]
        support = {**model_set["support"][str(horizon)], "applicability_policy": APPLICABILITY_POLICY}
        pairs = np.maximum(frame["anchor_pairs_q21"].to_numpy(), frame["anchor_pairs_pak"].to_numpy()).astype(int)
        cache[horizon] = [applicability(row, support, int(p))
                          for row, p in zip(frame[SUPPORT_COLUMNS].to_numpy(dtype=float), pairs)]
    return cache[horizon]


def _design(ctx: Context, model_set: dict, horizon: int) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    cache = model_set.setdefault("_design", {})
    if horizon not in cache:
        frame = ctx.lab_frames[horizon]
        stage1 = {n: model_set["stage1"][n][str(horizon)] for n in model_set["stage1"]} if horizon else None
        inputs, fallback = stage2_inputs(frame, horizon, stage1)
        gate_ok = np.array([g["status"] == "ok" for g in _gates(ctx, model_set, horizon)])
        cache[horizon] = (inputs, fallback, gate_ok)
    return cache[horizon]


def _ordered(folds: dict) -> dict:
    return dict(sorted(((k, (pd.Timestamp(s), pd.Timestamp(e))) for k, (s, e) in folds.items()), key=lambda kv: kv[1][0]))


def _prior_folds(folds: dict, start: pd.Timestamp) -> dict:
    return {k: v for k, v in folds.items() if v[1] <= start}


def fit_procedure(ctx: Context, cutoff, folds: dict) -> dict:
    """Model set using only information published before ``cutoff``.

    ``folds`` (ending at or before ``cutoff``) are used for configuration
    choices, each scored by the model set of its own start (recursively), and
    for the out-of-fold residual quantiles.  Without folds the preregistered
    configuration is used and no residual quantiles exist.
    """
    cutoff = pd.Timestamp(cutoff)
    folds = _ordered(folds)
    if any(end > cutoff for _, end in folds.values()):
        raise ValueError("a configuration fold ends after the cutoff")
    key = (cutoff, tuple(folds.items()))
    if key in ctx.procedures:
        return ctx.procedures[key]
    outer = {name: fit_procedure(ctx, start, _prior_folds(folds, start)) for name, (start, _) in folds.items()}
    names = [n for n in ANALYSER_METRICS if n in ctx.dynamics.analysers]
    stage1_selection = {n: {str(h): select_stage1(ctx.dynamics, n, h, cutoff, folds, ctx.stage1_cache)
                            for h in STAGE1_HORIZONS} for n in names}
    stage1 = {n: {str(h): ctx.stage1_model(n, h, per_h[str(h)]["training_filter"], per_h[str(h)]["alpha"], cutoff)
                  for h in STAGE1_HORIZONS} for n, per_h in stage1_selection.items()}
    model_set = {"cutoff": cutoff.isoformat(), "folds": {k: [s.isoformat(), e.isoformat()] for k, (s, e) in folds.items()},
                 "stage1": stage1, "stage1_selection": stage1_selection,
                 "support": {str(h): fit_support(ctx, h, cutoff) for h in HORIZONS_MINUTES},
                 "stage2": {}, "persistence": {}}
    for horizon in HORIZONS_MINUTES:
        key_h = str(horizon)
        y_ln = ctx.y_ln[horizon]
        if not folds:
            kind, alpha = PREREGISTERED_STAGE2["kind"], PREREGISTERED_STAGE2["alpha"]
            selection = {"basis": "preregistered"}
        else:
            scores = {}
            for config in STAGE2_CONFIGS:
                per_fold = {}
                for name, (start, end) in folds.items():
                    inputs, fallback, gate_ok = _design(ctx, outer[name], horizon)
                    fit = ctx.fit_rows(horizon, start) & gate_ok
                    score = ctx.fold_rows(horizon, start, end) & gate_ok & ~fallback
                    if fit.sum() < MIN_SUPPORT_ROWS or not score.any():
                        continue
                    model = fit_stage2(config[0], inputs[fit], y_ln[fit], config[1])
                    per_fold[name] = float(np.mean(np.abs(y_ln[score] - stage2_predict_frame(model, inputs[score]))))
                scores[config] = per_fold
            usable = {c: v for c, v in scores.items() if v}
            if not usable:
                raise ValueError(f"no scorable fold for horizon {horizon} before {cutoff}")
            best = min(usable, key=lambda c: float(np.mean(list(usable[c].values()))))
            kind, alpha = best[0], best[1] if best[1] is not None else PREREGISTERED_STAGE2["alpha"]
            selection = {"basis": "earlier_folds", "folds": list(folds),
                         "fold_ln_mae": {f"{c[0]}/{c[1]:g}" if c[1] else c[0]: v for c, v in scores.items()}}
        inputs, fallback, gate_ok = _design(ctx, model_set, horizon)
        fit = ctx.fit_rows(horizon, cutoff) & gate_ok
        model = fit_stage2(kind, inputs[fit], y_ln[fit], alpha)
        persistence = fit_stage2("convex", persistence_inputs(ctx.lab_frames[horizon], horizon)[fit], y_ln[fit])
        persistence["label"] = "persistence"
        model.update({"selection": {**selection, "kind": kind, "alpha": alpha if kind == "linear" else None},
                      "fit_rows": int(fit.sum()), "alarm_probability": ALARM_PROBABILITY,
                      "alarm_probability_basis": "fixed before evaluation (protocol), not optimised"})
        model_set["stage2"][key_h] = model
        model_set["persistence"][key_h] = persistence
    # Residual quantiles: only out-of-fold predictions of the outer model sets.
    model_set["_outer_predictions"] = {}
    for horizon in HORIZONS_MINUTES:
        key_h = str(horizon)
        stage2_residuals, persistence_residuals, per_fold = [], [], {}
        for name, (start, end) in folds.items():
            pred = predict_rows(ctx, outer[name], horizon, ctx.fold_rows(horizon, start, end))
            pred["fold"] = name
            model_set["_outer_predictions"][(name, horizon)] = pred
            ok = pred["forecast_status"].eq("ok")
            y = ln(pred["target"])
            stage2_residuals.append((y - pred["ln_prediction"])[ok & pred["path"].eq("stage2")])
            persistence_residuals.append((y - pred["ln_persistence"])[ok])
            per_fold[name] = int((ok & pred["path"].eq("stage2")).sum())
        if folds:
            model_set["stage2"][key_h]["residual_quantiles"] = residual_quantile_function(np.concatenate(stage2_residuals))
            model_set["stage2"][key_h]["residual_quantiles_persistence"] = residual_quantile_function(np.concatenate(persistence_residuals))
            model_set["stage2"][key_h]["residual_basis"] = {"source": "outer out-of-fold predictions", "rows_per_fold": per_fold}
    model_set["deployable"] = bool(folds)
    ctx.procedures[key] = model_set
    return model_set


def predict_rows(ctx: Context, model_set: dict, horizon: int, rows: np.ndarray) -> pd.DataFrame:
    """Predictions of ``model_set`` for the selected lab rows, exactly as the runtime serves them."""
    frame = ctx.lab_frames[horizon]
    index = np.flatnonzero(rows)
    key = str(horizon)
    inputs, fallback, _ = _design(ctx, model_set, horizon)
    gates = _gates(ctx, model_set, horizon)
    part = frame.iloc[index]
    stage2 = model_set["stage2"][key]
    ln_stage2 = stage2_predict_frame(stage2, inputs.iloc[index])
    ln_persist = predict_convex(model_set["persistence"][key], persistence_inputs(part, horizon).to_numpy(dtype=float))
    fb = fallback[index]
    ln_used = np.where(fb, ln_persist, ln_stage2)
    status, reasons = [], []
    for i, value in zip(index, ln_used):
        gate = gates[i]
        why = list(gate["reasons"])
        if gate["status"] == "ok" and not (np.isfinite(value) and value < 50):
            why.append("nonfinite_model_output")
        status.append("abstain" if why else "ok")
        reasons.append("|".join(why))
    status = np.array(status)
    quantiles = [stage2.get("residual_quantiles_persistence" if f else "residual_quantiles") for f in fb]
    ok = status == "ok"

    def per_row(fn):
        return [fn(q, v) if (o and q is not None) else np.nan for q, v, o in zip(quantiles, ln_used, ok)]

    out = pd.DataFrame({
        "horizon_minutes": horizon, "prediction_origin": part["prediction_origin"].to_numpy(),
        "target_time": part["target_time"].to_numpy(), "available_at": part["available_at"].to_numpy(),
        "target": part["target"].to_numpy(dtype=float), "model_cutoff": model_set["cutoff"],
        "forecast_status": status, "abstain_reasons": reasons,
        "stage1_status": np.where(fb, "fallback_persistence", "ok" if horizon else "not_applicable"),
        "path": np.where(fb, "persistence", "stage2"), "stage2_kind": stage2.get("label") or stage2.get("kind"),
        "ln_prediction": np.where(ok, ln_used, np.nan), "ln_stage2": ln_stage2, "ln_persistence": ln_persist,
        "prediction": np.where(ok, np.exp(ln_used), np.nan),
        "prediction_lower": per_row(lambda q, v: interval(q, v)[0]),
        "prediction_upper": per_row(lambda q, v: interval(q, v)[1]),
        "exceedance_probability": per_row(lambda q, v: exceedance_probability(q, v, np.log(HARD_LIMIT))),
        "baseline_previous_lab": part["previous_lab_available"].to_numpy(dtype=float),
        "baseline_level": np.exp(part["ln_level"].to_numpy(dtype=float)),
        "baseline_q21_anchored": np.exp(part["ln_q21"].to_numpy(dtype=float)),
        "baseline_pak_anchored": np.exp(part["ln_pak"].to_numpy(dtype=float)),
        "previous_lab_available_at": part["previous_lab_available_at"].to_numpy(),
        "anchor_pairs_q21": part["anchor_pairs_q21"].to_numpy(), "anchor_pairs_pak": part["anchor_pairs_pak"].to_numpy(),
    }, index=index)
    for column in inputs.columns:
        out[column] = inputs[column].to_numpy()[index]
    return out


def strip_diagnostics(model_set: dict) -> dict:
    """The serialisable part of a model set (drops caches and outer predictions)."""
    return {k: v for k, v in model_set.items() if not k.startswith("_")}


def _json_ready(model_set: dict) -> dict:
    return json.loads(json.dumps(strip_diagnostics(model_set), default=_json_default))


# ----------------------------------------------------------------------------
# Main benchmark
# ----------------------------------------------------------------------------

def window_metrics(pred: pd.DataFrame) -> dict:
    y = pred["target"].to_numpy(dtype=float)
    accepted = pred["forecast_status"].eq("ok").to_numpy() & np.isfinite(y)
    out = {"rows": int(len(pred)), "accepted_rows": int(accepted.sum()), "coverage": float(accepted.mean()) if len(pred) else None,
           "abstained_actual_above_10": int((~accepted & (y > HARD_LIMIT)).sum()),
           "fallback_rows": int((accepted & pred["path"].eq("persistence").to_numpy()).sum()),
           "model": regression_metrics(y[accepted], pred.loc[accepted, "prediction"]),
           "probability": probability_metrics(y[accepted], pred.loc[accepted, "exceedance_probability"], ALARM_PROBABILITY)}
    inside = (y >= pred["prediction_lower"]) & (y <= pred["prediction_upper"])
    out["interval_80_coverage"] = float(inside[accepted].mean()) if accepted.any() else None
    for name in ("previous_lab", "level", "q21_anchored", "pak_anchored"):
        out[f"baseline_{name}"] = regression_metrics(y[accepted], pred.loc[accepted, f"baseline_{name}"])
    return out


def run(directory: Path, output: Path, publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES,
        fit_ends: tuple[str, ...] = WALK_FORWARD_FIT_ENDS, folds: dict = SELECTION_FOLDS) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    analysers, controls, labs, extras, control_invalid = load_dataset(directory)
    print("dense features and lab frames", file=sys.stderr)
    ctx = build_context(analysers, controls, labs, publication_delay_minutes, control_invalid)
    folds = _ordered(folds)
    anomaly_frame = pd.DataFrame({tag: series.resample("10min").last() for tag, series in extras.items()})
    anomaly_frame = anomaly_frame.iloc[::3]
    anomaly_frame = anomaly_frame[regime_mask(pd.DataFrame({"T6": anomaly_frame.get("ht.T6"), "F9": anomaly_frame.get("ht.F9"),
                                                            "P13": anomaly_frame.get("ht.P13")}))]
    walk_forward, prediction_rows, manifest = [], [], {"folds": {}, "walk_forward": []}
    for fit_end_text in fit_ends:
        fit_end = pd.Timestamp(fit_end_text)
        fit_folds = _prior_folds(folds, fit_end)
        if not fit_folds:
            raise ValueError(f"no selection fold ends before {fit_end_text}")
        print(f"model set fit_end={fit_end_text}", file=sys.stderr)
        model_set = fit_procedure(ctx, fit_end, fit_folds)
        implied = {n: {h: stage1_implied_gains(m) for h, m in per_h.items()} for n, per_h in model_set["stage1"].items()}
        control_response = estimate_control_response(analysers.get("q21", pd.Series(dtype=float)), controls, fit_end)
        consistency = {}
        for control in CONTROL_METRICS:
            step = control_response.get(control, {}).get("coefficient")
            gains = [implied["q21"][h][control] for h in implied.get("q21", {})]
            gain = float(np.mean(gains)) if gains else None
            consistency[control] = {"step_event_coefficient": step, "stage1_implied_gain_mean": gain,
                                    "sign_agrees": bool(step is not None and gain is not None and np.sign(step) == np.sign(gain)),
                                    "ratio_stage1_over_step": float(gain / step) if step and gain is not None else None}
        anomaly = fit_anomaly_model(anomaly_frame, fit_end, {k: (s, e) for k, (s, e) in fit_folds.items()})
        position = fit_ends.index(fit_end_text)
        next_end = pd.Timestamp(fit_ends[position + 1]) if position + 1 < len(fit_ends) else None
        metrics, leakage = {}, {}
        for horizon in HORIZONS_MINUTES:
            origin = ctx.origin[horizon]
            window = (origin >= fit_end) if next_end is None else ((origin >= fit_end) & (origin < next_end))
            pred = predict_rows(ctx, model_set, horizon, window.to_numpy() & np.isfinite(ctx.y_ln[horizon]))
            pred["model_fit_end"] = fit_end_text
            pred["split"] = "walk_forward"
            prediction_rows.append(pred)
            metrics[str(horizon)] = window_metrics(pred)
            fit = ctx.fit_rows(horizon, fit_end)
            frame = ctx.lab_frames[horizon]
            leakage[str(horizon)] = {
                "fit_label_latest_available_at": ctx.available[horizon][fit].max().isoformat(),
                "labels_published_after_fit_end_in_fit": int((ctx.available[horizon][fit] >= fit_end).sum()),
                "previous_lab_published_after_origin": int((pd.to_datetime(frame["previous_lab_available_at"]) > ctx.origin[horizon]).sum()),
                "origin_horizon_mismatch": int((ctx.origin[horizon] + pd.Timedelta(minutes=horizon) != pd.to_datetime(frame["target_time"])).sum()),
                "window_origins_before_fit_end": int((pred["prediction_origin"] < fit_end).sum()),
            }
            if horizon:
                for name in model_set["stage1"]:
                    choice = model_set["stage1_selection"][name][str(horizon)]
                    rows = ctx.dynamics.rows(name, horizon, choice["training_filter"], before=fit_end)
                    leakage[str(horizon)][f"stage1_{name}_target_windows_after_fit_end"] = int(
                        (ctx.dynamics.window_end[horizon][rows] >= fit_end).sum())
            bad = sum(v for k, v in leakage[str(horizon)].items() if k != "fit_label_latest_available_at")
            leakage[str(horizon)]["passed"] = bad == 0
            if bad:
                raise AssertionError({"fit_end": fit_end_text, "horizon": horizon, **leakage[str(horizon)]})
        entry = _json_ready(model_set)
        entry.update({"fit_end": fit_end.isoformat(), "selection_folds": list(fit_folds), "available_from": fit_end.isoformat(),
                      "stage1_implied_gains": implied, "control_response": control_response, "consistency": consistency,
                      "anomaly": anomaly,
                      "train_median": float(np.median(labs.loc[(pd.to_datetime(labs["target_time"])
                                                                + pd.Timedelta(minutes=publication_delay_minutes) < fit_end).to_numpy(), "target"]))})
        walk_forward.append({"entry": entry, "metrics": metrics, "leakage_check": leakage})
        manifest["walk_forward"].append({"fit_end": fit_end.isoformat(), "configuration_folds": list(fit_folds),
                                         "labels_published_before": fit_end.isoformat(),
                                         "evaluation_window": [fit_end.isoformat(), next_end.isoformat() if next_end is not None else "end of data"],
                                         "role": ("retrospective audit (seen in earlier work)" if fit_end >= pd.Timestamp(AUDIT_START)
                                                  else "development year (decision)")})
    for name, (start, end) in folds.items():
        prior = _prior_folds(folds, start)
        manifest["folds"][name] = {"start": start.isoformat(), "end": end.isoformat(),
                                   "outer_model_cutoff": start.isoformat(), "outer_model_configured_on": list(prior),
                                   "configuration": "preregistered" if not prior else "earlier folds",
                                   "row_rule": "origin >= start and label published (sample + delay) < end; "
                                               "Stage-1 rows: target window ends < end"}
    outer_rows = []
    for key, model_set in ctx.procedures.items():
        for (fold, horizon), pred in model_set.get("_outer_predictions", {}).items():
            outer_rows.append(pred.assign(split=f"outer_{fold}", model_fit_end=pred["model_cutoff"]))
    outer = pd.concat(outer_rows).drop_duplicates(["split", "horizon_minutes", "prediction_origin"]) if outer_rows else pd.DataFrame()
    predictions = pd.concat([*prediction_rows, outer], ignore_index=True)
    predictions.to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")
    crossings = crossing_report(ctx.dynamics, folds)
    artifact = {
        "version": ARTIFACT_VERSION,
        "model": "two-stage v4r: dense analyser dynamics ridge (Stage 1) + lab-anchored convex/robust calibration (Stage 2); "
                 "nested walk-forward",
        "target": {"metric_id": TARGET_METRIC, "point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
        "hard_limit": HARD_LIMIT, "horizons_minutes": list(HORIZONS_MINUTES),
        "lims_publication_delay_minutes": float(publication_delay_minutes), "lab_anchor_samples": LAB_ANCHOR_SAMPLES,
        "feature_columns": SUPPORT_COLUMNS, "dynamics_columns": DYNAMICS_COLUMNS,
        "stage2_inputs": {"0": STAGE2_INPUTS[0], "h": STAGE2_INPUTS["h"]},
        "analyser_metrics": ANALYSER_METRICS, "control_metrics": CONTROL_METRICS,
        "applicability_policy": APPLICABILITY_POLICY, "stage1_regime": {k: list(v) for k, v in STAGE1_REGIME.items()},
        "quiet_future": {**QUIET_FUTURE, "semantics": "whole path (origin, origin+h+30min], gaps > 30 min are not quiet"},
        "alarm_probability": ALARM_PROBABILITY, "anomaly_tags": ANOMALY_TAGS,
        "model_available_from": pd.Timestamp(fit_ends[0]).isoformat(),
        "walk_forward": [w["entry"] for w in walk_forward],
    }
    (output / "model.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=1, default=_json_default), encoding="utf-8")
    (output / "fold_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {
        "dataset": str(directory), "observations_sha256": sha256(directory / "observations.parquet"),
        "target": artifact["target"], "lab_rows": int(len(labs)),
        "dense_rows": int(len(ctx.dynamics.origins)), "dense_regime_rows": int(ctx.dynamics.regime.sum()),
        "quiet_fraction_by_horizon": {h: float(ctx.dynamics.quiet[h][ctx.dynamics.regime].mean()) for h in STAGE1_HORIZONS},
        "walk_forward_fit_ends": list(fit_ends), "selection_folds": {k: [s.isoformat(), e.isoformat()] for k, (s, e) in folds.items()},
        "alarm_probability": ALARM_PROBABILITY, "stage1_target_window_crossings_removed": crossings,
        "walk_forward": [{"fit_end": w["entry"]["fit_end"], "metrics": w["metrics"], "leakage_check": w["leakage_check"],
                          "stage1_selection": {n: {h: {k: v for k, v in c.items() if k in ("training_filter", "alpha", "basis", "quiet_fraction", "folds")}
                                                   for h, c in per_h.items()} for n, per_h in w["entry"]["stage1_selection"].items()},
                          "stage2_selection": {h: m["selection"] for h, m in w["entry"]["stage2"].items()},
                          "stage2_residual_basis": {h: m.get("residual_basis") for h, m in w["entry"]["stage2"].items()},
                          "control_response": w["entry"]["control_response"], "consistency": w["entry"]["consistency"]}
                         for w in walk_forward],
        "model_artifact": "model.json", "model_artifact_sha256": sha256(output / "model.json"),
    }
    (output / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lims-publication-delay-hours", type=float, default=4.0)
    args = parser.parse_args()
    metadata = run(args.dataset.resolve(), args.output.resolve(), args.lims_publication_delay_hours * 60)
    summary = {e["fit_end"][:10]: {h: {"n": m["model"].get("n"), "mae": m["model"].get("mae"),
                                        "previous_lab_mae": m["baseline_previous_lab"].get("mae"),
                                        "brier": m["probability"].get("brier"), "coverage": m["coverage"]}
                                    for h, m in e["metrics"].items()} for e in metadata["walk_forward"]}
    print(json.dumps({"output": str(args.output.resolve()), "summary": summary}, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
