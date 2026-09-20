"""Reproducible first iteration for forecasting hydro-treatment sulphur.

The script reads the source package outside the repository and writes only small
derived artefacts. For a target sample at t the origin is t minus the forecast
horizon. Telemetry is available through the origin; a previous lab sample is
usable only after its independent publication delay. This benchmark makes no
causal claim about changing setpoints.

Example (PowerShell)::

    python tools/modeling/sulfur_first_iteration.py \
      --source C:\\Users\\Roma\\Desktop\\нефтекод\\hakathon-data \
      --output reports/modeling/sulfur-first-iteration
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:
    from .sulfur_features import (APPLICABILITY_POLICY, FORECAST_HORIZON_MINUTES,
                                 LIMS_PUBLICATION_DELAY_MINUTES, MAX_LAB_AGE_MINUTES,
                                 applicability, available_lab, telemetry_features)
except ImportError:  # Direct execution: python tools/modeling/sulfur_first_iteration.py
    from sulfur_features import (APPLICABILITY_POLICY, FORECAST_HORIZON_MINUTES,
                                 LIMS_PUBLICATION_DELAY_MINUTES, MAX_LAB_AGE_MINUTES,
                                 applicability, available_lab, telemetry_features)


TARGET_POINT = "Гидроочистка"
TARGET_PARAMETER = "Mg.Sulfur"
DEFAULT_HORIZON = pd.Timedelta(minutes=FORECAST_HORIZON_MINUTES)
ALPHA_GRID = (30.0, 100.0, 300.0, 1000.0, 3000.0)


class StandardizedRidge:
    """Small dependency-free Ridge implementation for this benchmark.

    Imputation and scaling statistics are fitted on the training rows only;
    keeping them in this object makes the leakage boundary explicit.
    """

    def __init__(self, alpha: float = 10.0):
        self.alpha = float(alpha)

    def fit(self, frame: pd.DataFrame, target: pd.Series) -> "StandardizedRidge":
        self.feature_columns_ = list(frame.columns)
        values = frame.to_numpy(dtype=float)
        self.support_lower_ = np.nanquantile(values, APPLICABILITY_POLICY["support_quantiles"][0], axis=0)
        self.support_upper_ = np.nanquantile(values, APPLICABILITY_POLICY["support_quantiles"][1], axis=0)
        self.medians_ = np.nanmedian(values, axis=0)
        self.medians_[~np.isfinite(self.medians_)] = 0.0
        values = np.where(np.isfinite(values), values, self.medians_)
        self.mean_ = values.mean(axis=0)
        self.scale_ = values.std(axis=0)
        self.scale_[~np.isfinite(self.scale_) | (self.scale_ == 0)] = 1.0
        margin = APPLICABILITY_POLICY["support_margin_fraction"] * np.maximum(
            self.support_upper_ - self.support_lower_, self.scale_)
        self.support_lower_ = np.nan_to_num(self.support_lower_ - margin)
        self.support_upper_ = np.nan_to_num(self.support_upper_ + margin)
        standardized = (values - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(standardized)), standardized])
        regularizer = np.eye(design.shape[1]) * self.alpha
        regularizer[0, 0] = 0.0
        self.coef_ = np.linalg.solve(design.T @ design + regularizer, design.T @ target.to_numpy(dtype=float))
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if hasattr(self, "feature_columns_"):
            missing = [column for column in self.feature_columns_ if column not in frame.columns]
            if missing:
                raise ValueError(f"missing model features: {missing[:5]}")
            frame = frame.loc[:, self.feature_columns_]
        values = frame.to_numpy(dtype=float)
        values = np.where(np.isfinite(values), values, self.medians_)
        standardized = (values - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(standardized)), standardized])
        return design @ self.coef_

    def artifact(self) -> dict:
        """Return a JSON-safe inference artifact for the runtime service.

        The artifact contains only fitted parameters and feature ordering.  It
        deliberately does not contain source observations, so the production
        endpoint can reconstruct an as-of feature vector from its own Parquet
        dataset while preserving the training availability boundary.
        """
        if not hasattr(self, "feature_columns_"):
            raise ValueError("fit the model before exporting an artifact")
        return {
            "version": 2,
            "model": "standardized_ridge_log1p",
            "alpha": self.alpha,
            "feature_columns": self.feature_columns_,
            "medians": self.medians_.tolist(),
            "mean": self.mean_.tolist(),
            "scale": self.scale_.tolist(),
            "coef": self.coef_.tolist(),
            "support_lower": self.support_lower_.tolist(),
            "support_upper": self.support_upper_.tolist(),
            "applicability_policy": APPLICABILITY_POLICY,
        }


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, (pd.Timedelta,)):
        return value.total_seconds()
    raise TypeError(f"cannot serialise {type(value)!r}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_telemetry(source: Path) -> pd.DataFrame:
    """Load both process CSVs without modifying them.

    Columns receive a stage prefix because the two files reuse tag names.  The
    resulting index is the source ``date`` and remains at the native 10-minute
    frequency.  No interpolation or forward fill is performed here.
    """
    frames: list[pd.DataFrame] = []
    for path in sorted((source / "data").glob("*_tags.csv")):
        stage = path.stem.removesuffix("_tags")
        frame = pd.read_csv(path, usecols=lambda c: not c.startswith("Unnamed"))
        if "date" not in frame:
            raise ValueError(f"{path} does not contain date")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce", format="mixed")
        frame = frame.dropna(subset=["date"]).set_index("date").sort_index()
        frame = frame.apply(pd.to_numeric, errors="coerce").astype("float64")
        if frame.index.has_duplicates:
            # Match importer conflict handling instead of choosing an arbitrary duplicate.
            conflict = frame.groupby(level=0).nunique(dropna=False) > 1
            frame = frame.groupby(level=0).last().mask(conflict)
        frame.columns = [f"{stage}__{column}" for column in frame.columns]
        frames.append(frame)
    if not frames:
        raise FileNotFoundError(f"no *_tags.csv files in {source / 'data'}")
    telemetry = pd.concat(frames, axis=1, join="outer").sort_index()
    return telemetry


def load_lab_target(source: Path) -> pd.DataFrame:
    """Extract hydro-treatment point 2 ``Mg.Sulfur`` from the LIMS workbook."""
    candidates = sorted(source.glob("ЛИМС*.xlsx"))
    if not candidates:
        raise FileNotFoundError("LIMS workbook (ЛИМС*.xlsx) not found")
    raw = pd.read_excel(candidates[0], header=None)
    point: object = None
    found: list[pd.DataFrame] = []
    for col in range(0, raw.shape[1] - 1, 2):
        if pd.notna(raw.iloc[0, col]):
            point = str(raw.iloc[0, col])
        parameter = str(raw.iloc[1, col]) if pd.notna(raw.iloc[1, col]) else ""
        # The source has a small punctuation typo ("Гидроочистка'.."), so
        # match the stable semantic parts instead of an exact point string.
        if (TARGET_PARAMETER not in parameter or TARGET_POINT not in str(point)
                or "Точка отбора '2'" not in str(point)):
            continue
        part = pd.DataFrame({
            "target_time": pd.to_datetime(raw.iloc[4:, col], errors="coerce", format="mixed"),
            "target": pd.to_numeric(raw.iloc[4:, col + 1], errors="coerce"),
        }).dropna(subset=["target_time", "target"])
        part["target_time"] = pd.to_datetime(part["target_time"])
        found.append(part)
    if not found:
        raise ValueError("hydro-treatment point 2 Mg.Sulfur column was not found")
    target = (pd.concat(found, ignore_index=True)
              .sort_values("target_time")
              .drop_duplicates("target_time", keep="last")
              .reset_index(drop=True))
    return target


def add_past_features(telemetry: pd.DataFrame) -> pd.DataFrame:
    """Add past-only rolling means; current values are retained as features."""
    result = telemetry.copy()
    # Time-based rolling never sees a future row.  min_periods=1 keeps the
    # early history usable while the imputer handles missing sensor cells.
    for window, suffix in [("1h", "mean_1h"), ("6h", "mean_6h")]:
        rolling = telemetry.rolling(window, min_periods=1).mean()
        rolling.columns = [f"{c}__{suffix}" for c in rolling.columns]
        result = pd.concat([result, rolling], axis=1)
    return result


def align_target_features(target: pd.DataFrame, telemetry: pd.DataFrame,
                           forecast_horizon: pd.Timedelta) -> pd.DataFrame:
    """Build windows at each exact prediction origin, including off-grid times."""
    left = target.copy()
    left["prediction_origin"] = left["target_time"] - forecast_horizon
    left["feature_cutoff"] = left["prediction_origin"]
    features, latest = telemetry_features(telemetry, pd.DatetimeIndex(left["prediction_origin"]))
    aligned = pd.concat([left.reset_index(drop=True), features], axis=1)
    aligned["feature_time"] = latest
    return aligned


def time_split(frame: pd.DataFrame, train_end: str = "2025-01-01",
               validation_end: str = "2026-01-01",
               publication_delay: pd.Timedelta = pd.Timedelta(minutes=LIMS_PUBLICATION_DELAY_MINUTES)) -> dict[str, pd.Series]:
    """Purge boundary rows until training/selection labels are actually published."""
    times = pd.to_datetime(frame["target_time"])
    origins = pd.to_datetime(frame.get("prediction_origin", times - DEFAULT_HORIZON))
    label_available = times + publication_delay
    train_boundary = pd.Timestamp(train_end)
    validation_boundary = pd.Timestamp(validation_end)
    masks = {
        "train": label_available < train_boundary,
        "validation": (origins >= train_boundary) & (label_available < validation_boundary),
        "test": origins >= validation_boundary,
    }
    if any(int(mask.sum()) == 0 for mask in masks.values()):
        raise ValueError({name: int(mask.sum()) for name, mask in masks.items()})
    if bool((masks["train"] & masks["validation"]).any() or
            (masks["validation"] & masks["test"]).any() or
            (masks["train"] & masks["test"]).any()):
        raise AssertionError("time split masks overlap")
    return masks


def regression_metrics(y_true: Iterable[float], y_pred: Iterable[float], threshold: float = 10.0) -> dict:
    actual = np.asarray(list(y_true), dtype=float)
    predicted = np.asarray(list(y_pred), dtype=float)
    if not len(actual):
        return {"n": 0, "mae": None, "rmse": None, "median_absolute_error": None,
                "bias_pred_minus_actual": None, "actual_above_10": 0, "predicted_above_10": 0,
                "precision_above_10": None, "recall_above_10": None, "false_alarm_rate": None}
    error = predicted - actual
    dangerous = actual > threshold
    alarm = predicted > threshold
    tp = int((dangerous & alarm).sum())
    fp = int((~dangerous & alarm).sum())
    fn = int((dangerous & ~alarm).sum())
    return {
        "n": int(len(actual)),
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error ** 2))),
        "median_absolute_error": float(np.median(np.abs(error))),
        "bias_pred_minus_actual": float(np.mean(error)),
        "actual_above_10": int(dangerous.sum()),
        "predicted_above_10": int(alarm.sum()),
        "precision_above_10": float(tp / (tp + fp)) if tp + fp else None,
        "recall_above_10": float(tp / (tp + fn)) if tp + fn else None,
        "false_alarm_rate": float(fp / max(1, (~dangerous).sum())),
    }


def run(source: Path, output: Path, forecast_horizon: pd.Timedelta = DEFAULT_HORIZON,
        publication_delay: pd.Timedelta = pd.Timedelta(minutes=LIMS_PUBLICATION_DELAY_MINUTES)) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    telemetry = load_telemetry(source)
    target = load_lab_target(source)
    aligned = align_target_features(target, telemetry, forecast_horizon)
    lab = available_lab(target, pd.DatetimeIndex(aligned["prediction_origin"]), publication_delay.total_seconds() / 60)
    aligned = pd.concat([aligned, lab], axis=1)
    masks = time_split(aligned, publication_delay=publication_delay)

    y = aligned["target"].astype(float)
    train_mask = masks["train"]
    feature_columns = list(telemetry.columns)
    feature_columns += [f"{c}__mean_1h" for c in telemetry.columns]
    feature_columns += [f"{c}__mean_6h" for c in telemetry.columns]
    feature_columns += ["previous_lab_available"]
    X = aligned[feature_columns]
    # Sulphur has a highly skewed contamination/error tail (one 2120 mg/kg
    # value).  Log1p training keeps the first-pass model numerically stable;
    # metrics are reported after transforming back to mg/kg.  Select alpha on
    # validation only; test is not consulted during model selection.
    validation_mask = masks["validation"]
    alpha_scores: dict[str, float] = {}
    for alpha in ALPHA_GRID:
        candidate = StandardizedRidge(alpha=alpha)
        candidate.fit(X.loc[train_mask], np.log1p(y.loc[train_mask]))
        candidate_prediction = np.maximum(0.0, np.expm1(candidate.predict(X.loc[validation_mask])))
        alpha_scores[str(alpha)] = regression_metrics(y.loc[validation_mask], candidate_prediction)["mae"]
    selected_alpha = min(ALPHA_GRID, key=lambda alpha: alpha_scores[str(alpha)])
    model = StandardizedRidge(alpha=selected_alpha)
    model.fit(X.loc[train_mask], np.log1p(y.loc[train_mask]))
    artifact = model.artifact()
    support = [applicability(row, artifact) for row in X.to_numpy(dtype=float)]
    aligned["forecast_status"] = [s["status"] for s in support]
    aligned["abstain_reasons"] = ["|".join(s["reasons"]) for s in support]
    aligned["missing_fraction"] = [s["missing_fraction"] for s in support]
    aligned["ood_fraction"] = [s["ood_fraction"] for s in support]
    aligned["prediction_ridge"] = np.maximum(0.0, np.expm1(model.predict(X)))
    aligned["prediction_previous_lab"] = aligned["previous_lab_available"]
    # A numerical regressor is too conservative around the hard 10 mg/kg
    # alarm threshold.  Keep a separate, auditable risk signal: when an
    # already-available lab result exists, never lower it with the smoother
    # Ridge estimate.  This is an alarm guard, not a second quality forecast.
    aligned["prediction_risk_guard"] = np.maximum(
        aligned["prediction_ridge"],
        aligned["prediction_previous_lab"].fillna(-np.inf),
    )

    metrics: dict[str, dict] = {}
    for split_name, mask in masks.items():
        valid_baseline = mask & aligned["prediction_previous_lab"].notna()
        accepted = mask & aligned["forecast_status"].eq("ok")
        comparable = accepted & aligned["prediction_previous_lab"].notna()
        metrics[split_name] = {
            "ridge": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_ridge"]),
            "previous_lab": regression_metrics(y.loc[valid_baseline], aligned.loc[valid_baseline, "prediction_previous_lab"]),
            "risk_guard": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_risk_guard"]),
            "rows_without_previous_lab": int((mask & ~aligned["prediction_previous_lab"].notna()).sum()),
            "coverage": float(accepted.sum() / mask.sum()),
            "accepted_rows": int(accepted.sum()),
            "abstained_rows": int((mask & ~accepted).sum()),
            "abstained_actual_above_10": int((mask & ~accepted & y.gt(10)).sum()),
            "accepted_ridge": regression_metrics(y.loc[accepted], aligned.loc[accepted, "prediction_ridge"]),
            "accepted_risk_guard": regression_metrics(y.loc[accepted], aligned.loc[accepted, "prediction_risk_guard"]),
            "comparable_ridge": regression_metrics(y.loc[comparable], aligned.loc[comparable, "prediction_ridge"]),
            "comparable_previous_lab": regression_metrics(y.loc[comparable], aligned.loc[comparable, "prediction_previous_lab"]),
        }
    year_metrics: dict[str, dict] = {}
    years = pd.to_datetime(aligned["target_time"]).dt.year
    for year in sorted(years.unique()):
        mask = years.eq(year)
        valid_baseline = mask & aligned["prediction_previous_lab"].notna()
        year_metrics[str(int(year))] = {
            "ridge": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_ridge"]),
            "previous_lab": regression_metrics(y.loc[valid_baseline], aligned.loc[valid_baseline, "prediction_previous_lab"]),
            "risk_guard": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_risk_guard"]),
        }

    # Explicit availability/leakage checks become part of the report, not just
    # an informal assumption in the notebook.
    telemetry_violations = int((aligned["feature_time"] > aligned["prediction_origin"]).sum())
    lab_violations = int((aligned["previous_lab_available_at"] > aligned["prediction_origin"]).sum())
    train_labels_latest = (aligned.loc[masks["train"], "target_time"] + publication_delay).max()
    validation_origin_earliest = aligned.loc[masks["validation"], "prediction_origin"].min()
    selection_labels_latest = (aligned.loc[masks["validation"], "target_time"] + publication_delay).max()
    test_origin_earliest = aligned.loc[masks["test"], "prediction_origin"].min()
    split_violations = int(train_labels_latest >= validation_origin_earliest) + int(selection_labels_latest >= test_origin_earliest)
    leakage = {
        "rows": int(len(aligned)),
        "violating_rows": telemetry_violations + lab_violations,
        "telemetry_violations": telemetry_violations,
        "lab_publication_violations": lab_violations,
        "split_availability_violations": split_violations,
        "train_label_latest_available_at": train_labels_latest,
        "validation_earliest_origin": validation_origin_earliest,
        "validation_label_latest_available_at": selection_labels_latest,
        "test_earliest_origin": test_origin_earliest,
        "minimum_lag_minutes": float((aligned["target_time"] - aligned["feature_time"]).dt.total_seconds().min() / 60),
        "required_lag_minutes": float(forecast_horizon.total_seconds() / 60),
        "passed": telemetry_violations + lab_violations + split_violations == 0,
    }
    if not leakage["passed"]:
        raise AssertionError(f"feature availability violation: {leakage}")

    predictions = aligned[["prediction_origin", "target_time", "feature_cutoff", "feature_time", "target",
                           "previous_lab_sample_time", "previous_lab_available_at", "previous_lab_available",
                           "prediction_previous_lab", "prediction_ridge", "prediction_risk_guard",
                           "forecast_status", "abstain_reasons", "missing_fraction", "ood_fraction"]].copy()
    predictions["split"] = "purged_boundary"
    for split_name, mask in masks.items():
        predictions.loc[mask, "split"] = split_name
    predictions.to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")
    artifact = model.artifact()
    artifact.update(
        {
            "target": {"point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
            "forecast_horizon_minutes": float(forecast_horizon.total_seconds() / 60),
            "lims_publication_delay_minutes": float(publication_delay.total_seconds() / 60),
            "max_lab_age_minutes": MAX_LAB_AGE_MINUTES,
            "feature_engineering": "current valid value + means over (origin-1h/6h,origin] + lab sample whose publication <=origin",
            "risk_guard": "max(prediction_ridge, previous_lab_available)",
            "selected_alpha": selected_alpha,
            "model_available_from": "2026-01-01T00:00:00",
        }
    )
    (output / "model.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    metadata = {
        "source": str(source),
        "source_files": {str(p.relative_to(source)): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                         for p in sorted(source.rglob("*"))
                         if p.is_file() and not p.name.startswith("~$")},
        "target": {"point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
        "forecast_horizon_minutes": float(forecast_horizon.total_seconds() / 60),
        "lims_publication_delay_minutes": float(publication_delay.total_seconds() / 60),
        "max_lab_age_minutes": MAX_LAB_AGE_MINUTES,
        "telemetry_rows": int(len(telemetry)),
        "telemetry_signals": int(len(telemetry.columns)),
        "target_rows": int(len(target)),
        "aligned_rows": int(len(aligned)),
        "feature_count": int(len(feature_columns)),
        "feature_engineering": artifact["feature_engineering"] + "; train-only median imputation; shared exact-origin runtime implementation",
        "model": "standardized Ridge on log1p(target), inverse transformed for metrics; risk_guard=max(Ridge, available previous lab)",
        "alpha_grid": list(ALPHA_GRID),
        "selected_alpha": selected_alpha,
        "alpha_selection": {"split": "validation", "metric": "MAE", "scores": alpha_scores},
        "split_boundaries": {"train_end_exclusive": "2025-01-01", "validation_end_exclusive": "2026-01-01"},
        "split_rows": {name: int(mask.sum()) for name, mask in masks.items()},
        "purged_boundary_rows": int(len(aligned) - sum(mask.sum() for mask in masks.values())),
        "applicability_policy": APPLICABILITY_POLICY,
        "metrics_by_year": year_metrics,
        "leakage_check": leakage,
        "metrics": metrics,
        "model_artifact": "model.json",
        "model_artifact_sha256": sha256(output / "model.json"),
    }
    (output / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(output / "REPORT.md", metadata)
    return metadata


def write_report(path: Path, metadata: dict) -> None:
    metrics = metadata["metrics"]
    rows = [
        "# Первая итерация прогноза серы",
        "",
        "## Что проверено",
        "",
        f"Цель: `{TARGET_POINT}`, `{TARGET_PARAMETER}`, мг/кг. Использованы {metadata['aligned_rows']} лабораторных наблюдений, сопоставленных с {metadata['feature_count']} признаками (291 телеметрический признак и доступный предыдущий лабораторный результат).",
        "",
        f"Контракт: прогноз выпускается в `prediction_origin` на **{metadata['forecast_horizon_minutes']:.0f} минут** вперёд (`target_time`). Телеметрия доступна до origin включительно. ЛИМС доступен только при `sample_time + {metadata['lims_publication_delay_minutes']:.0f} минут <= origin`. Давность опубликованного ЛИМС ограничена 48 часами. Задержка публикации не является горизонтом прогноза.",
        "Окна средних `(origin−1h, origin]` и `(origin−6h, origin]` считаются на точном времени origin, даже между десятиминутными отсчётами. Последнее валидное значение каждого канала должно быть не старше 30 минут. Offline и runtime используют одну реализацию признаков.",
        "",
        f"Разбиение по времени учитывает публикацию обучающих ответов: train labels должны быть опубликованы до 2025-01-01; validation origin начинается 2025-01-01, а ответы опубликованы до 2026-01-01; test origin начинается 2026-01-01. Исключено {metadata['purged_boundary_rows']} пограничных строк. Лабораторная цель не интерполировалась.",
        f"Гиперпараметр Ridge выбран только по MAE на validation из сетки {metadata['alpha_grid']}; выбранное значение: **{metadata['selected_alpha']}**. Test не использовался при выборе.",
        "",
        "## Метрики всех строк (диагностика сырого регрессора до abstain)",
        "",
        "| split | модель | n | MAE | RMSE | median AE | bias | recall >10 | false alarm |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for split in ("train", "validation", "test"):
        for name, label in (("ridge", "Ridge"), ("previous_lab", "предыдущая проба"), ("risk_guard", "risk guard")):
            m = metrics[split][name]
            rows.append(f"| {split} | {label} | {m['n']} | {m['mae']:.3f} | {m['rmse']:.3f} | {m['median_absolute_error']:.3f} | {m['bias_pred_minus_actual']:.3f} | {m['recall_above_10'] if m['recall_above_10'] is not None else '—'} | {m['false_alarm_rate']:.3f} |")
    rows += [
        "", "## Область применимости и честное покрытие", "",
        "Правила abstain зафиксированы до оценки: более 20% пропущенных признаков, отсутствие телеметрии, более 10% доступных признаков за train-only областью либо |z|>12. Область: train-квантили 0.5–99.5%, расширенные на 10% размаха. Это инженерные эвристики, не калиброванная уверенность и не допустимые границы управления.",
        "", "| split | покрытие | abstain | >10 среди abstain | n сравнения | Ridge MAE | baseline MAE | recall принятого Ridge >10 |", "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for split, m in metrics.items():
        ridge = m["comparable_ridge"]
        baseline = m["comparable_previous_lab"]
        rows.append(f"| {split} | {m['coverage']:.1%} | {m['abstained_rows']} | {m['abstained_actual_above_10']} | {ridge['n']} | {ridge['mae']} | {baseline['mae']} | {m['accepted_ridge']['recall_above_10']} |")
    rows += [
        "",
        "## Стабильность по годам",
        "",
        "| год | n | Ridge MAE | baseline MAE | Ridge RMSE | baseline RMSE |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for year, year_metric in metadata["metrics_by_year"].items():
        ridge = year_metric["ridge"]
        baseline = year_metric["previous_lab"]
        rows.append(f"| {year} | {ridge['n']} | {ridge['mae']:.3f} | {baseline['mae']:.3f} | {ridge['rmse']:.3f} | {baseline['rmse']:.3f} |")
    rows += [
        "",
        "## Проверка утечки",
        "",
        f"Телеметрия: {metadata['leakage_check']['telemetry_violations']} нарушений; публикация ЛИМС: {metadata['leakage_check']['lab_publication_violations']}; границы обучения/выбора alpha: {metadata['leakage_check']['split_availability_violations']}. Проверка: **{'пройдена' if metadata['leakage_check']['passed'] else 'не пройдена'}**.",
        "",
        "## Интерпретация и ограничения",
        "",
        "Предыдущая версия метрик использовала неверную доступность ЛИМС и заменена этим отчётом. Численное качество и полнота тревог оцениваются отдельно: малый MAE не доказывает обнаружение превышений. `risk_guard=max(Ridge, доступный ЛИМС)` сохраняет высокий лабораторный результат; это эвристический сигнал, не вероятность превышения. В runtime при abstain численный прогноз не публикуется; predictions.csv сохраняет сырой регрессор для аудита вместе со статусом. Сравнение после abstain проводится на одинаковых строках с доступным baseline. После выбора alpha по 2025 году артефакт доступен только с 2026-01-01; runtime отказывается от исторического прогноза до этой даты. Train/validation — диагностические метрики, независимая оценка только test. Эффект изменения управляющих воздействий и промышленная безопасность этим benchmark не доказываются.",
        f"SHA-256 артефакта: `{metadata['model_artifact_sha256']}`.",
        "",
        "Артефакты: `model.json` (параметры для runtime-инференса), `metrics.json` (параметры, хэши источников, метрики), `predictions.csv` (малый файл результатов). Исходные CSV/XLSX в репозиторий не копируются.",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--forecast-horizon-hours", type=float, default=3.0)
    parser.add_argument("--lims-publication-delay-hours", type=float, default=4.0)
    args = parser.parse_args()
    if not 0 < args.forecast_horizon_hours <= 3 or args.lims_publication_delay_hours <= 0:
        parser.error("forecast horizon must be in (0, 3] hours; LIMS delay must be positive")
    metadata = run(args.source.resolve(), args.output.resolve(), pd.Timedelta(hours=args.forecast_horizon_hours),
                   pd.Timedelta(hours=args.lims_publication_delay_hours))
    print(json.dumps({"output": str(args.output.resolve()), "metrics": metadata["metrics"], "leakage_check": metadata["leakage_check"]}, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
