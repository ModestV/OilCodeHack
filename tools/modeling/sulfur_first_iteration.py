"""Reproducible first iteration for forecasting hydro-treatment sulphur.

The script reads the source package outside the repository and writes only small
derived artefacts.  It deliberately models the availability boundary: for a
laboratory sample at ``t`` only process values at or before ``t - lag`` are
used.  This is an offline benchmark, not a causal claim about changing setpoints.

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


TARGET_POINT = "Гидроочистка"
TARGET_PARAMETER = "Mg.Sulfur"
DEFAULT_LAG = pd.Timedelta(hours=4)
ALPHA_GRID = (30.0, 100.0, 300.0, 1000.0, 3000.0)


class StandardizedRidge:
    """Small dependency-free Ridge implementation for this benchmark.

    Imputation and scaling statistics are fitted on the training rows only;
    keeping them in this object makes the leakage boundary explicit.
    """

    def __init__(self, alpha: float = 10.0):
        self.alpha = float(alpha)

    def fit(self, frame: pd.DataFrame, target: pd.Series) -> "StandardizedRidge":
        values = frame.to_numpy(dtype=float)
        self.medians_ = np.nanmedian(values, axis=0)
        self.medians_[~np.isfinite(self.medians_)] = 0.0
        values = np.where(np.isfinite(values), values, self.medians_)
        self.mean_ = values.mean(axis=0)
        self.scale_ = values.std(axis=0)
        self.scale_[~np.isfinite(self.scale_) | (self.scale_ == 0)] = 1.0
        standardized = (values - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(standardized)), standardized])
        regularizer = np.eye(design.shape[1]) * self.alpha
        regularizer[0, 0] = 0.0
        self.coef_ = np.linalg.solve(design.T @ design + regularizer, design.T @ target.to_numpy(dtype=float))
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        values = frame.to_numpy(dtype=float)
        values = np.where(np.isfinite(values), values, self.medians_)
        standardized = (values - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(standardized)), standardized])
        return design @ self.coef_


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
        frame = frame.loc[~frame.index.duplicated(keep="last")]
        frame = frame.apply(pd.to_numeric, errors="coerce").astype("float32")
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
                           availability_lag: pd.Timedelta) -> pd.DataFrame:
    """As-of join target rows to the last available process row before the lag."""
    left = target.copy()
    left["feature_cutoff"] = left["target_time"] - availability_lag
    right = telemetry.reset_index(names="feature_time").sort_values("feature_time")
    aligned = pd.merge_asof(
        left.sort_values("feature_cutoff"), right,
        left_on="feature_cutoff", right_on="feature_time", direction="backward",
        tolerance=pd.Timedelta("30min"),
    )
    aligned = aligned.dropna(subset=["feature_time"]).sort_values("target_time").reset_index(drop=True)
    return aligned


def time_split(frame: pd.DataFrame, train_end: str = "2025-01-01",
               validation_end: str = "2026-01-01") -> dict[str, pd.Series]:
    """Return mutually exclusive chronological masks."""
    times = pd.to_datetime(frame["target_time"])
    train_boundary = pd.Timestamp(train_end)
    validation_boundary = pd.Timestamp(validation_end)
    masks = {
        "train": times < train_boundary,
        "validation": (times >= train_boundary) & (times < validation_boundary),
        "test": times >= validation_boundary,
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


def run(source: Path, output: Path, availability_lag: pd.Timedelta) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    telemetry = load_telemetry(source)
    features = add_past_features(telemetry)
    target = load_lab_target(source)
    aligned = align_target_features(target, features, availability_lag)
    masks = time_split(aligned)

    y = aligned["target"].astype(float)
    train_mask = masks["train"]
    # A previous lab result is available only after the same cutoff.  A plain
    # row shift would leak a result from a sample taken less than four hours
    # before the prediction origin.
    target_times = target["target_time"].to_numpy(dtype="datetime64[ns]")
    prior_index = np.searchsorted(
        target_times, aligned["feature_cutoff"].to_numpy(dtype="datetime64[ns]"), side="right"
    ) - 1
    target_values = target["target"].to_numpy(dtype=float)
    aligned["previous_lab_available"] = np.where(
        prior_index >= 0, target_values[np.maximum(prior_index, 0)], np.nan
    )
    feature_columns = [c for c in features.columns if c in aligned.columns] + ["previous_lab_available"]
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
        metrics[split_name] = {
            "ridge": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_ridge"]),
            "previous_lab": regression_metrics(y.loc[valid_baseline], aligned.loc[valid_baseline, "prediction_previous_lab"]),
            "risk_guard": regression_metrics(y.loc[mask], aligned.loc[mask, "prediction_risk_guard"]),
            "rows_without_previous_lab": int((mask & ~aligned["prediction_previous_lab"].notna()).sum()),
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
    leakage = {
        "rows": int(len(aligned)),
        "violating_rows": int((aligned["feature_time"] > aligned["feature_cutoff"]).sum()),
        "minimum_lag_minutes": float((aligned["target_time"] - aligned["feature_time"]).dt.total_seconds().min() / 60),
        "required_lag_minutes": float(availability_lag.total_seconds() / 60),
        "passed": bool((aligned["feature_time"] <= aligned["feature_cutoff"]).all()),
    }
    if not leakage["passed"]:
        raise AssertionError(f"feature availability violation: {leakage}")

    predictions = aligned[["target_time", "feature_time", "target", "previous_lab_available", "prediction_previous_lab", "prediction_ridge", "prediction_risk_guard"]].copy()
    predictions.to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")
    metadata = {
        "source": str(source),
        "source_files": {str(p.relative_to(source)): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                         for p in sorted(source.rglob("*"))
                         if p.is_file() and not p.name.startswith("~$")},
        "target": {"point": TARGET_POINT, "parameter": TARGET_PARAMETER, "unit": "мг/кг"},
        "availability_lag": str(availability_lag),
        "telemetry_rows": int(len(telemetry)),
        "telemetry_signals": int(len(telemetry.columns)),
        "target_rows": int(len(target)),
        "aligned_rows": int(len(aligned)),
        "feature_count": int(len(feature_columns)),
        "feature_engineering": "current value + past-only 1h/6h rolling means + previous lab available at cutoff; train-only median imputation",
        "model": "standardized Ridge on log1p(target), inverse transformed for metrics; risk_guard=max(Ridge, available previous lab)",
        "alpha_grid": list(ALPHA_GRID),
        "selected_alpha": selected_alpha,
        "alpha_selection": {"split": "validation", "metric": "MAE", "scores": alpha_scores},
        "split_boundaries": {"train_end_exclusive": "2025-01-01", "validation_end_exclusive": "2026-01-01"},
        "split_rows": {name: int(mask.sum()) for name, mask in masks.items()},
        "metrics_by_year": year_metrics,
        "leakage_check": leakage,
        "metrics": metrics,
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
        f"Признак доступен только не позднее чем за **{metadata['leakage_check']['required_lag_minutes']:.0f} минут** до времени отбора. В признаки вошли текущие значения, средние за 1 и 6 часов (только прошлые строки) и предыдущий лабораторный результат, если он уже доступен к этому cutoff.",
        "",
        "Разбиение по времени: train до 2025-01-01, validation — 2025 год, test — с 2026-01-01. Лабораторная цель не протягивалась вперёд и не интерполировалась.",
        f"Гиперпараметр Ridge выбран только по MAE на validation из сетки {metadata['alpha_grid']}; выбранное значение: **{metadata['selected_alpha']}**. Test не использовался при выборе.",
        "",
        "## Метрики",
        "",
        "| split | модель | n | MAE | RMSE | median AE | bias | recall >10 | false alarm |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for split in ("train", "validation", "test"):
        for name, label in (("ridge", "Ridge"), ("previous_lab", "предыдущая проба"), ("risk_guard", "risk guard")):
            m = metrics[split][name]
            rows.append(f"| {split} | {label} | {m['n']} | {m['mae']:.3f} | {m['rmse']:.3f} | {m['median_absolute_error']:.3f} | {m['bias_pred_minus_actual']:.3f} | {m['recall_above_10'] if m['recall_above_10'] is not None else '—'} | {m['false_alarm_rate']:.3f} |")
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
        f"Нарушений доступности: **{metadata['leakage_check']['violating_rows']}** из {metadata['leakage_check']['rows']}; минимальный фактический лаг: {metadata['leakage_check']['minimum_lag_minutes']:.1f} минут; проверка: **{'пройдена' if metadata['leakage_check']['passed'] else 'не пройдена'}**.",
        "",
        "## Интерпретация и ограничения",
        "",
        "Ridge на log1p-цели сопоставим с сильным baseline по MAE, но не заменяет его без дополнительной проверки по режимам. `risk_guard` намеренно сохраняет доступный высокий лабораторный результат для порогового предупреждения; его метрики нельзя читать как независимый прогноз. Это benchmark предсказательной ценности, а не доказательство причинного эффекта изменения режима. Вне области исторических признаков и при плохом качестве датчиков система должна переходить в abstain. Экстремумы не удалялись автоматически.",
        "",
        "Артефакты: `metrics.json` (параметры, хэши источников, метрики), `predictions.csv` (малый файл результатов). Исходные CSV/XLSX в репозиторий не копируются.",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--availability-lag-hours", type=float, default=4.0)
    args = parser.parse_args()
    metadata = run(args.source.resolve(), args.output.resolve(), pd.Timedelta(hours=args.availability_lag_hours))
    print(json.dumps({"output": str(args.output.resolve()), "metrics": metadata["metrics"], "leakage_check": metadata["leakage_check"]}, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
