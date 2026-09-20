"""Reproducible lab-anchored sulphur nowcast/forecast for hydro-treated diesel.

Reads the imported dataset (``storage/<id>/observations.parquet``) so that the
offline benchmark and the runtime endpoint share one cleaning path.  For each
horizon ``h`` in ``HORIZONS_MINUTES`` a lab sample at ``t`` defines an origin
``t-h``; features use only data known at the origin (telemetry <= origin,
LIMS published <= origin).  Model selection uses 2024-2025 labels; 2026 is a
retrospective audit (already examined in prior development).  The benchmark makes no causal claim: control
responses are estimated separately from analyser step events and are
reported as scenario defaults with their evidence.

Example::

    python tools/modeling/sulfur_forecast.py --dataset storage/hackathon \
        --output reports/modeling/sulfur-forecast
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.modeling.sulfur_features import (  # noqa: E402
    ANALYSER_METRICS, APPLICABILITY_POLICY, CONTROL_METRICS, FEATURE_COLUMNS, HORIZONS_MINUTES,
    LAB_ANCHOR_SAMPLES, LIMS_PUBLICATION_DELAY_MINUTES, MODEL_COLUMNS, applicability, build_features, clean_analyser,
    exceedance_probability, interval, ln, residual_quantile_function,
)

TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
TARGET_POINT = "Гидроочистка"
TARGET_PARAMETER = "Mg.Sulfur"
HARD_LIMIT = 10.0
ALPHA_GRID = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0, 10000.0, 100000.0)
FOLD_YEARS = (2024, 2025)
VALIDATION_END = "2026-01-01"
ALARM_PROBABILITY_GRID = (0.2, 0.25, 0.3, 0.35, 0.4, 0.5)


class StandardizedRidge:
    """Dependency-free ridge on standardized features with train-only imputation."""

    def __init__(self, alpha: float):
        self.alpha = float(alpha)

    def fit(self, frame: pd.DataFrame, target: np.ndarray) -> "StandardizedRidge":
        self.feature_columns_ = list(frame.columns)
        values = frame.to_numpy(dtype=float)
        lo, hi = APPLICABILITY_POLICY["support_quantiles"]
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
        values = frame.loc[:, self.feature_columns_].to_numpy(dtype=float)
        values = np.where(np.isfinite(values), values, self.medians_)
        standardized = (values - self.mean_) / self.scale_
        return np.column_stack([np.ones(len(standardized)), standardized]) @ self.coef_

    def artifact(self) -> dict:
        return {"feature_columns": self.feature_columns_, "alpha": self.alpha,
                "medians": self.medians_.tolist(), "mean": self.mean_.tolist(), "scale": self.scale_.tolist(),
                "coef": self.coef_.tolist(), "support_lower": self.support_lower_.tolist(),
                "support_upper": self.support_upper_.tolist()}


def predict_from_artifact(model: dict, raw: np.ndarray) -> float:
    filled = np.where(np.isfinite(raw), raw, np.asarray(model["medians"]))
    standardized = (filled - np.asarray(model["mean"])) / np.asarray(model["scale"])
    return float(np.array([1.0, *standardized]) @ np.asarray(model["coef"]))


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


def load_dataset(directory: Path) -> tuple[dict[str, pd.Series], dict[str, pd.Series], pd.DataFrame]:
    """Load cleaned analysers, control telemetry and the lab target from Parquet."""
    path = str(directory / "observations.parquet").replace("'", "''")
    wanted = list(ANALYSER_METRICS.values()) + list(CONTROL_METRICS.values()) + [TARGET_METRIC]
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
    return analysers, controls, labs


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
        ranks = np.empty(len(order)); ranks[order] = np.arange(1, len(order) + 1)
        # average ranks for ties
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


def estimate_control_response(analyser: pd.Series, controls: dict[str, pd.Series],
                              fit_end_exclusive: str = VALIDATION_END) -> dict:
    """Median log-sulphur response to step changes of each control from analyser data.

    Events are 1-hour changes of one control beyond a threshold while the
    other controls stay quiet and the unit is in a normal regime.  The
    response is the median over events of ``(ln S(t+k) - ln S(t-1h..t)) / step``
    for k = 1..6 hours.  Operators also move controls *because* sulphur
    drifts, so this is a lagged observational response, not a proven causal
    gain; the direction for temperature matches the expert statement.
    """
    # Bind log values to their timestamps, even if controls have different indices.
    frame = pd.DataFrame({"lnS": pd.Series(ln(analyser), index=analyser.index),
                          **{k: v for k, v in controls.items()}}).sort_index()
    frame = frame.loc[frame.index < pd.Timestamp(fit_end_exclusive)]
    frame = frame.resample("10min").last()
    normal = (frame["T6"] > 320) & (frame["T6"] < 400) & (frame["F9"] > 100) & (frame["P13"] > 3)
    lnS = frame["lnS"].where(normal)
    specs = {
        "T6": {"threshold": 3.0, "relative": False, "unit": "ln(mg/kg) per °C"},
        "F9": {"threshold": 8.0, "relative": True, "unit": "ln(mg/kg) per % of feed"},
        "P13": {"threshold": 0.15, "relative": False, "unit": "ln(mg/kg) per MPa"},
    }
    result = {}
    for name, spec in specs.items():
        series = frame[name]
        step = (series / series.shift(6) - 1) * 100 if spec["relative"] else series - series.shift(6)
        quiet = pd.Series(True, index=frame.index)
        for other, other_spec in specs.items():
            if other == name:
                continue
            other_step = (frame[other] / frame[other].shift(6) - 1) * 100 if other_spec["relative"] else frame[other] - frame[other].shift(6)
            quiet &= other_step.abs() < other_spec["threshold"] / 3
        events = np.where((step.abs() >= spec["threshold"]) & normal & quiet)[0]
        responses = []
        values = lnS.to_numpy()
        steps = step.to_numpy()
        for i in events:
            if i - 6 < 0 or i + 39 >= len(values):
                continue
            base = np.nanmean(values[i - 6:i])
            trajectory = [np.nanmean(values[i + k * 6 - 3:i + k * 6 + 3]) - base for k in range(1, 7)]
            if np.isfinite(base) and np.all(np.isfinite(trajectory)):
                responses.append(np.asarray(trajectory) / steps[i])
        responses = np.asarray(responses)
        if not len(responses):
            result[name] = {"events": 0, "response_by_hour": None, "coefficient": None, "unit": spec["unit"]}
            continue
        by_hour = np.median(responses, axis=0)
        plateau = float(np.median(by_hour[1:4]))  # 2-4 hours: after the transport lag
        result[name] = {"events": int(len(responses)), "step_threshold": spec["threshold"], "relative_step": spec["relative"],
                        "response_by_hour": {f"{k}h": float(by_hour[k - 1]) for k in range(1, 7)},
                        "coefficient": plateau, "unit": spec["unit"],
                        "lag_minutes": int(60 * (1 + int(np.argmax(np.abs(by_hour[:3]) >= 0.8 * abs(plateau)))))}
    return result


def temporal_folds(target_time: pd.Series, origins: pd.Series, publication_delay: pd.Timedelta) -> dict[str, tuple[pd.Series, pd.Series]]:
    """Expanding temporal folds for model selection; labels must be published before use.

    ``2024``: fit on labels published before 2024-01-01, score origins in 2024;
    ``2025``: fit on labels published before 2025-01-01, score origins in 2025.
    ``final`` fits on every label published before ``VALIDATION_END``; origins
    from ``VALIDATION_END`` are the retrospective audit.
    """
    available = pd.to_datetime(target_time) + publication_delay
    origins = pd.to_datetime(origins)
    folds = {}
    for year in FOLD_YEARS:
        start, end = pd.Timestamp(f"{year}-01-01"), pd.Timestamp(f"{year + 1}-01-01")
        folds[str(year)] = (available < start, (origins >= start) & (available < end))
    boundary = pd.Timestamp(VALIDATION_END)
    folds["final"] = (available < boundary, origins >= boundary)
    for name, (fit, score) in folds.items():
        if not fit.any() or not score.any() or (fit & score).any():
            raise ValueError(f"fold {name} is empty or overlaps")
    return folds


def run(directory: Path, output: Path, publication_delay_minutes: float = LIMS_PUBLICATION_DELAY_MINUTES) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    analysers, controls, labs = load_dataset(directory)
    publication_delay = pd.Timedelta(minutes=publication_delay_minutes)
    horizons: dict[str, dict] = {}
    predictions_all = []
    train_median = None
    for horizon in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs["target_time"]) - pd.Timedelta(minutes=horizon)
        frame = build_features(analysers, controls, labs, origins, publication_delay_minutes)
        frame["target_time"] = labs["target_time"].to_numpy()
        frame["target"] = labs["target"].to_numpy()
        frame["horizon_minutes"] = horizon
        folds = temporal_folds(frame["target_time"], frame["prediction_origin"], publication_delay)
        y_ln = ln(frame["target"])
        y = frame["target"].to_numpy(dtype=float)
        fit_mask, test_mask = folds["final"]
        # Alpha: mean out-of-fold log-MAE over the temporal folds.  Log error
        # keeps a single contaminated 2120 mg/kg sample from dominating.
        selection_scores: dict[str, float] = {}
        oof_residual = np.full(len(frame), np.nan)
        oof_ln_pred = np.full(len(frame), np.nan)
        oof_accepted = np.zeros(len(frame), dtype=bool)
        for alpha in ALPHA_GRID:
            scores = []
            for year in FOLD_YEARS:
                fit, score = folds[str(year)]
                candidate = StandardizedRidge(alpha).fit(frame.loc[fit, MODEL_COLUMNS], y_ln[fit])
                pred = candidate.predict(frame.loc[score])
                scores.append(float(np.mean(np.abs(y_ln[score] - pred))))
            selection_scores[str(alpha)] = float(np.mean(scores))
        alpha = min(ALPHA_GRID, key=lambda a: selection_scores[str(a)])
        for year in FOLD_YEARS:
            fit, score = folds[str(year)]
            candidate = StandardizedRidge(alpha).fit(frame.loc[fit, MODEL_COLUMNS], y_ln[fit])
            pred = candidate.predict(frame.loc[score])
            oof_ln_pred[score.to_numpy()] = pred
            oof_residual[score.to_numpy()] = y_ln[score] - pred
            fold_support = StandardizedRidge(alpha).fit(frame.loc[fit, FEATURE_COLUMNS], y_ln[fit]).artifact()
            for i in frame.index[score]:
                check = applicability(frame.loc[i, FEATURE_COLUMNS].to_numpy(float),
                                      {**fold_support, "applicability_policy": APPLICABILITY_POLICY},
                                      int(max(frame.loc[i, "anchor_pairs_q21"], frame.loc[i, "anchor_pairs_pak"])))
                oof_accepted[i] = check["status"] == "ok"
        model = StandardizedRidge(alpha).fit(frame.loc[fit_mask, MODEL_COLUMNS], y_ln[fit_mask])
        ln_pred = model.predict(frame)
        # Intervals and P(>10) come from out-of-fold residuals (2024-2025),
        # never from in-sample residuals of the final fit.
        quantile_function = residual_quantile_function(oof_residual[oof_accepted])
        # Applicability is judged on the full feature vector (analysers, lab
        # anchor and control regime), fitted on the same rows.
        support_model = StandardizedRidge(alpha).fit(frame.loc[fit_mask, FEATURE_COLUMNS], y_ln[fit_mask]).artifact()
        support = [applicability(row, {**support_model, "applicability_policy": APPLICABILITY_POLICY},
                                 int(max(frame.loc[i, "anchor_pairs_q21"], frame.loc[i, "anchor_pairs_pak"])))
                   for i, row in enumerate(frame[FEATURE_COLUMNS].to_numpy(dtype=float))]
        frame["forecast_status"] = [s["status"] for s in support]
        frame["abstain_reasons"] = ["|".join(s["reasons"]) for s in support]
        frame["prediction"] = np.exp(ln_pred)
        frame["prediction_lower"] = [interval(quantile_function, v)[0] for v in ln_pred]
        frame["prediction_upper"] = [interval(quantile_function, v)[1] for v in ln_pred]
        frame["exceedance_probability"] = [exceedance_probability(quantile_function, v, np.log(HARD_LIMIT)) for v in ln_pred]
        frame["oof_prediction"] = np.exp(oof_ln_pred)
        frame["oof_exceedance_probability"] = [exceedance_probability(quantile_function, v, np.log(HARD_LIMIT)) if np.isfinite(v) else np.nan
                                              for v in oof_ln_pred]
        train_median = float(np.median(y[fit_mask]))
        frame["baseline_constant"] = train_median
        frame["baseline_previous_lab"] = frame["previous_lab_available"]
        frame["baseline_level"] = np.exp(frame["ln_level"])
        frame["baseline_q21_anchored"] = np.exp(frame["ln_q21"])
        frame["baseline_pak_anchored"] = np.exp(frame["ln_pak"])
        # Alarm threshold: best F1 of out-of-fold P(>10) on the selection folds.
        selection_rows = np.isfinite(oof_ln_pred) & oof_accepted
        alarm_scores = {}
        for probability in ALARM_PROBABILITY_GRID:
            m = probability_metrics(y[selection_rows], frame.loc[selection_rows, "oof_exceedance_probability"], probability)
            recall, precision = m.get("recall_above_10") or 0.0, m.get("precision_above_10") or 0.0
            alarm_scores[str(probability)] = float(2 * recall * precision / (recall + precision)) if recall + precision else 0.0
        alarm_probability = max(ALARM_PROBABILITY_GRID, key=lambda p: alarm_scores[str(p)])
        splits = {"selection_2024": folds["2024"][1], "selection_2025": folds["2025"][1], "test": test_mask}
        metrics = {}
        for split_name, mask in splits.items():
            accepted = mask & (frame["forecast_status"].eq("ok") if split_name == "test" else oof_accepted)
            column, probability_column = ("prediction", "exceedance_probability") if split_name == "test" else ("oof_prediction", "oof_exceedance_probability")
            entry = {
                "rows": int(mask.sum()), "accepted_rows": int(accepted.sum()), "coverage": float(accepted.sum() / max(1, mask.sum())),
                "abstained_actual_above_10": int((mask & ~accepted & (frame["target"] > HARD_LIMIT)).sum()),
                "evaluation": "retrospective audit 2026 (previously examined)" if split_name == "test" else "out-of-fold (fitted on earlier years only)",
                "model": regression_metrics(y[mask], frame.loc[mask, column]),
                "accepted_model": regression_metrics(y[accepted], frame.loc[accepted, column]),
                "accepted_probability": probability_metrics(y[accepted], frame.loc[accepted, probability_column], alarm_probability),
                "interval_80_coverage": float(((y >= frame["prediction_lower"]) & (y <= frame["prediction_upper"]))[accepted].mean()) if accepted.any() and split_name == "test" else None,
            }
            for name in ("constant", "previous_lab", "level", "q21_anchored", "pak_anchored"):
                baseline_values = frame.loc[accepted, f"baseline_{name}"]
                if name == "constant" and split_name != "test":
                    fold_fit = folds[split_name.removeprefix("selection_")][0]
                    baseline_values = np.full(int(accepted.sum()), np.median(y[fold_fit]))
                entry[f"baseline_{name}"] = regression_metrics(y[accepted], baseline_values)
            metrics[split_name] = entry
        years = pd.to_datetime(frame["target_time"]).dt.year
        by_year = {}
        for year in sorted(years.unique()):
            mask = years.eq(year) & frame["forecast_status"].eq("ok")
            by_year[str(int(year))] = {"n": int(mask.sum()), "in_sample": bool(year < pd.Timestamp(VALIDATION_END).year),
                                       "model_mae": regression_metrics(y[mask], frame.loc[mask, "prediction"]).get("mae"),
                                       "constant_mae": regression_metrics(y[mask], frame.loc[mask, "baseline_constant"]).get("mae"),
                                       "previous_lab_mae": regression_metrics(y[mask], frame.loc[mask, "baseline_previous_lab"]).get("mae")}
        lab_violation = int((pd.to_datetime(frame["previous_lab_available_at"]) > pd.to_datetime(frame["prediction_origin"])).sum())
        origin_violation = int((pd.to_datetime(frame["prediction_origin"]) + pd.Timedelta(minutes=horizon) != pd.to_datetime(frame["target_time"])).sum())
        fit_label_latest = (pd.to_datetime(frame.loc[fit_mask, "target_time"]) + publication_delay).max()
        test_origin_earliest = pd.to_datetime(frame.loc[test_mask, "prediction_origin"]).min()
        split_violation = int(fit_label_latest >= test_origin_earliest)
        horizons[str(horizon)] = {
            "horizon_minutes": horizon, "selected_alpha": alpha, "selection_scores_log_mae": selection_scores,
            "alarm_probability": alarm_probability, "alarm_probability_f1": alarm_scores,
            "fit_rows": int(fit_mask.sum()), "test_rows": int(test_mask.sum()),
            "coefficients": dict(zip(["intercept", *MODEL_COLUMNS], model.coef_.tolist())),
            "metrics": metrics, "metrics_by_year": by_year,
            "leakage_check": {"origin_horizon_mismatch_rows": origin_violation, "lab_publication_violations": lab_violation,
                              "fit_label_latest_available_at": fit_label_latest, "test_earliest_origin": test_origin_earliest,
                              "split_violations": split_violation,
                              "passed": origin_violation + lab_violation + split_violation == 0},
            "artifact": {**model.artifact(), "support": support_model,
                         "residual_quantiles": quantile_function, "alarm_probability": alarm_probability},
        }
        if not horizons[str(horizon)]["leakage_check"]["passed"]:
            raise AssertionError(horizons[str(horizon)]["leakage_check"])
        frame["split"] = "purged_boundary"
        for split_name, mask in splits.items():
            frame.loc[mask, "split"] = split_name
        frame.loc[fit_mask & ~folds["2024"][1] & ~folds["2025"][1], "split"] = "fit_only"
        predictions_all.append(frame)
    predictions = pd.concat(predictions_all, ignore_index=True)
    columns = ["horizon_minutes", "prediction_origin", "target_time", "target", "split", "forecast_status", "abstain_reasons",
               "prediction", "prediction_lower", "prediction_upper", "exceedance_probability", "oof_prediction", "oof_exceedance_probability",
               "baseline_constant", "baseline_previous_lab", "baseline_level", "baseline_q21_anchored", "baseline_pak_anchored",
               "previous_lab_sample_time", "previous_lab_available_at", "previous_lab_available",
               "offset_q21", "offset_pak", "anchor_pairs_q21", "anchor_pairs_pak", *FEATURE_COLUMNS]
    predictions[columns].to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")

    control_response = estimate_control_response(analysers.get("q21", pd.Series(dtype=float)), controls)
    artifact = {
        "version": 3,
        "model": "lab-anchored standardized ridge on ln(sulfur) per horizon; empirical out-of-fold residual quantiles",
        "target": {"metric_id": TARGET_METRIC, "point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
        "hard_limit": HARD_LIMIT,
        "horizons_minutes": list(HORIZONS_MINUTES),
        "lims_publication_delay_minutes": float(publication_delay_minutes),
        "lab_anchor_samples": LAB_ANCHOR_SAMPLES,
        "lab_level_samples": 5,
        "feature_contract": "causal_plateau_sample_age_v1",
        "control_fit_end_exclusive": VALIDATION_END,
        "calibration_note": "Accepted OOF 2024-2025; support fitted inside each fold. Alpha and alarm tuning reuse these folds; selection metrics are not independent validation.",
        "model_columns": MODEL_COLUMNS, "feature_columns": FEATURE_COLUMNS,
        "analyser_metrics": ANALYSER_METRICS, "control_metrics": CONTROL_METRICS,
        "applicability_policy": APPLICABILITY_POLICY,
        "models": {key: value["artifact"] for key, value in horizons.items()},
        "control_response": control_response,
        "train_median": train_median,
        "model_available_from": VALIDATION_END + "T00:00:00",
        "split_boundaries": {"selection_folds": list(FOLD_YEARS), "fit_end_exclusive": VALIDATION_END},
    }
    (output / "model.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    metadata = {
        "dataset": str(directory),
        "observations_sha256": sha256(directory / "observations.parquet"),
        "target": artifact["target"], "lab_rows": int(len(labs)),
        "analyser_valid_fraction": {k: float(v.notna().mean()) for k, v in analysers.items()},
        "model_columns": MODEL_COLUMNS, "feature_columns": FEATURE_COLUMNS,
        "alpha_grid": list(ALPHA_GRID), "alarm_probability_grid": list(ALARM_PROBABILITY_GRID),
        "split_boundaries": artifact["split_boundaries"],
        "horizons": {k: {kk: vv for kk, vv in v.items() if kk != "artifact"} for k, v in horizons.items()},
        "control_response": control_response,
        "model_artifact": "model.json", "model_artifact_sha256": sha256(output / "model.json"),
    }
    (output / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(output / "REPORT.md", metadata)
    return metadata


def _fmt(value, digits=3):
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def write_report(path: Path, metadata: dict) -> None:
    folds = metadata["split_boundaries"]["selection_folds"]
    rows = [
        "# Прогноз серы после гидроочистки: калиброванный по ЛИМС nowcast/forecast",
        "",
        "## Постановка",
        "",
        f"Цель: `{metadata['target']['metric_id']}` ({metadata['target']['point']}, точка 2, мг/кг), {metadata['lab_rows']} лабораторных проб. "
        "Прогноз на горизонты 0/60/120/180 минут от момента выпуска (origin). Телеметрия и анализаторы используются не позже origin; "
        f"результат ЛИМС — только при `sample_time + {LIMS_PUBLICATION_DELAY_MINUTES} мин <= origin`.",
        "",
        "Входы регрессии (только свидетельства о сере продукта): очищенные поточные анализаторы `Q21` (КИП) и ПАК — текущее значение и среднее за час, "
        f"скорректированные на медианное смещение «ЛИМС − анализатор» по последним {LAB_ANCHOR_SAMPLES} опубликованным пробам; "
        "локальный уровень (медиана последних 5 опубликованных проб); предыдущая опубликованная проба. "
        "Показания плато исключаются начиная с момента достижения 60 минут; ранние показания не удаляются задним числом. Значения вне (0, 100) исключены. Модель — ridge на ln(серы) с train-only нормировкой.",
        "",
        "Абсолютные уровни `T6/F9/P13` и 24-часовые средние анализаторов участвуют только в проверке области применимости (режим установки), "
        "а не в регрессии: они дрейфуют с возрастом катализатора и калибровкой анализатора, а выученные коэффициенты при изменениях `T6` "
        "кодируют реакцию оператора, а не физический отклик. Эти признаки ухудшали удержанный 2026 год, поэтому решение об их исключении "
        "принято после просмотра 2026: для выбора набора признаков 2026 не является полностью слепым тестом. Регуляризация α и порог тревоги "
        f"выбраны только на out-of-fold годах {folds} (расширяющиеся временные фолды, лог-MAE), финальная подгонка — на пробах, "
        f"опубликованных до {metadata['split_boundaries']['fit_end_exclusive']}; пробы 2026 не участвовали ни в подгонке, ни в отборе α. "
        "Интервалы и вероятность превышения 10 мг/кг — эмпирические квантили принятых out-of-fold остатков ln; gate обучен отдельно внутри каждого фолда. "
        "Выбор α, калибровка и подбор порога переиспользуют 2024–2025: метрики выбора не являются независимой проверкой вероятностей.",
        "",
        "## Точность по горизонтам (удержанный 2026, принятые строки)",
        "",
        "| горизонт | покрытие | n | MAE модели | MAE константы | MAE пред. пробы | MAE Q21 калибр. | corr | 80% интервал | Brier skill P(>10) | AUC | recall @порог | ложные тревоги |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, entry in metadata["horizons"].items():
        test = entry["metrics"]["test"]
        m, p = test["accepted_model"], test["accepted_probability"]
        rows.append(
            f"| {horizon} мин | {test['coverage']:.1%} | {m.get('n', 0)} | {_fmt(m.get('mae'))} | {_fmt(test['baseline_constant'].get('mae'))} | "
            f"{_fmt(test['baseline_previous_lab'].get('mae'))} | {_fmt(test['baseline_q21_anchored'].get('mae'))} | {_fmt(m.get('correlation'))} | "
            f"{_fmt(test.get('interval_80_coverage'), 2)} | {_fmt(p.get('brier_skill'))} | {_fmt(p.get('auc'))} | "
            f"{_fmt(p.get('recall_above_10'), 2)} @{p.get('alarm_probability')} | {_fmt(p.get('false_alarm_rate'), 2)} |"
        )
    rows += ["", f"## Out-of-fold {folds} (выбор α и порога тревоги; принятые строки)", "",
             "| горизонт | фолд | α | n | MAE модели | MAE константы | MAE пред. пробы | corr | Brier skill | AUC |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for horizon, entry in metadata["horizons"].items():
        for fold in folds:
            v = entry["metrics"][f"selection_{fold}"]
            m, p = v["accepted_model"], v["accepted_probability"]
            rows.append(f"| {horizon} мин | {fold} | {entry['selected_alpha']} | {m.get('n', 0)} | {_fmt(m.get('mae'))} | {_fmt(v['baseline_constant'].get('mae'))} | "
                        f"{_fmt(v['baseline_previous_lab'].get('mae'))} | {_fmt(m.get('correlation'))} | {_fmt(p.get('brier_skill'))} | {_fmt(p.get('auc'))} |")
    rows += ["", "## Стабильность по годам (принятые строки; до 2026 — in-sample финальной подгонки)", "",
             "| горизонт | " + " | ".join(f"{y}: n / MAE модели / константы" for y in next(iter(metadata["horizons"].values()))["metrics_by_year"]) + " |",
             "|---:|" + "---|" * len(next(iter(metadata["horizons"].values()))["metrics_by_year"])]
    for horizon, entry in metadata["horizons"].items():
        rows.append(f"| {horizon} мин | " + " | ".join(f"{d['n']} / {_fmt(d['model_mae'], 2)} / {_fmt(d['constant_mae'], 2)}" for d in entry["metrics_by_year"].values()) + " |")
    rows += ["", "## Коэффициенты (стандартизованные признаки, ln серы)", "", "| горизонт | " + " | ".join(["intercept", *MODEL_COLUMNS]) + " |",
             "|---:|" + "---:|" * (len(MODEL_COLUMNS) + 1)]
    for horizon, entry in metadata["horizons"].items():
        rows.append(f"| {horizon} мин | " + " | ".join(f"{v:+.3f}" for v in entry["coefficients"].values()) + " |")
    rows += ["", "## Отклик серы на управляющие воздействия (анализатор Q21, ступенчатые события)", "",
             "| тег | событий | коэффициент | единица | лаг, мин | отклик по часам |", "|---|---:|---:|---|---:|---|"]
    for name, item in metadata["control_response"].items():
        by_hour = item.get("response_by_hour") or {}
        rows.append(f"| {name} | {item.get('events', 0)} | {_fmt(item.get('coefficient'), 4)} | {item.get('unit')} | {item.get('lag_minutes', '—')} | "
                    + ", ".join(f"{k}: {v:+.4f}" for k, v in by_hour.items()) + " |")
    rows += [
        "",
        "Коэффициенты — медианный лагированный отклик ln(серы) на ступень одного управляющего тега (за час) при спокойных остальных тегах "
        "и нормальном режиме. Операторы меняют режим и в ответ на дрейф серы, поэтому это наблюдательная оценка, а не доказанный причинный эффект; "
        "направление для температуры совпадает с подтверждением эксперта. Значения используются как редактируемые умолчания сценарной модели.",
        "",
        "## Интерпретация",
        "",
        "- Суточная лабораторная проба содержит быструю (часовую) составляющую и шум анализа; за 3 часа вперёд предсказуема лишь малая её часть, "
        "поэтому на 180 мин MAE модели близка к MAE константы. Ценность прогноза на длинном горизонте — калиброванная вероятность превышения "
        "и интервал, однако их полезность также требует проверки: Brier skill около нуля не подтверждает преимущество перед частотой превышений.",
        "- На горизонте 0 (nowcast) калиброванный анализатор существенно точнее последней пробы и константы: именно он служит базой сценариев.",
        "- Строки со статусом abstain (нет анализатора и пробы, режим вне области обучения, мало пар для калибровки) не получают прогноза.",
        "- Метрики относятся к выходу гидроочистки, не к товарной смеси после блендинга.",
        "",
        f"SHA-256 артефакта: `{metadata['model_artifact_sha256']}`; SHA-256 наблюдений: `{metadata['observations_sha256']}`.",
        "",
        "Артефакты: `model.json` (модели по горизонтам, квантили остатков, отклики), `metrics.json`, `predictions.csv`.",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lims-publication-delay-hours", type=float, default=4.0)
    args = parser.parse_args()
    metadata = run(args.dataset.resolve(), args.output.resolve(), args.lims_publication_delay_hours * 60)
    summary = {h: {"test_mae": v["metrics"]["test"]["accepted_model"].get("mae"),
                   "test_constant_mae": v["metrics"]["test"]["baseline_constant"].get("mae"),
                   "test_auc": v["metrics"]["test"]["accepted_probability"].get("auc")} for h, v in metadata["horizons"].items()}
    print(json.dumps({"output": str(args.output.resolve()), "summary": summary, "control_response": metadata["control_response"]},
                     ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
