"""Leakage-safe runtime adapter for the first sulphur forecasting model.

The training script writes a small JSON artifact containing the fitted Ridge
parameters.  This module reconstructs the feature vector from the imported
LONG Parquet data using an as-of cutoff.  It intentionally shares the model's
availability contract (four hours before the LIMS sample) and never reads a
telemetry row after that cutoff.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from .analytics import parse_time
from .config import LIMS_PUBLICATION_DELAY_MINUTES, ROOT


ARTIFACT = ROOT / "reports" / "modeling" / "sulfur-first-iteration" / "model.json"
TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
TELEMETRY_STAGE_MAP = {"242000": "ht", "24-2000": "ht"}
FEATURE_TOLERANCE = timedelta(minutes=30)


class ForecastUnavailable(ValueError):
    """Raised when the model artifact or runtime dataset cannot be used."""


def _load_artifact(path: Path = ARTIFACT) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ForecastUnavailable("Артефакт модели прогноза серы не найден") from exc
    except json.JSONDecodeError as exc:
        raise ForecastUnavailable("Артефакт модели прогноза серы повреждён") from exc
    required = ("feature_columns", "medians", "mean", "scale", "coef")
    if artifact.get("version") != 1 or any(key not in artifact for key in required):
        raise ForecastUnavailable("Артефакт модели прогноза серы имеет неизвестный формат")
    size = len(artifact["feature_columns"])
    if any(len(artifact[key]) != size for key in ("medians", "mean", "scale")):
        raise ForecastUnavailable("Артефакт модели прогноза серы содержит несовместимые признаки")
    if len(artifact["coef"]) != size + 1:
        raise ForecastUnavailable("Артефакт модели прогноза серы содержит неверные коэффициенты")
    return artifact


def _base_metric(feature: str) -> str | None:
    """Map an offline feature name (``stage__tag__mean_1h``) to an API id."""

    if feature == "previous_lab_available":
        return None
    base = feature
    for suffix in ("__mean_1h", "__mean_6h"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if "__" not in base:
        return None
    stage, code = base.split("__", 1)
    stage = TELEMETRY_STAGE_MAP.get(stage, stage)
    return f"{stage}.{code}"


def _read_rows(directory: Path, metric_ids: list[str], cutoff, window_start):
    if not metric_ids:
        return []
    path = directory / "observations.parquet"
    if not path.exists():
        raise ForecastUnavailable("В наборе нет импортированных наблюдений")
    escaped = str(path).replace("'", "''")
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"""SELECT metric_id, timestamp, value, flags
                FROM read_parquet('{escaped}')
                WHERE source='kip'
                  AND metric_id IN (SELECT unnest(?))
                  AND timestamp > ? AND timestamp <= ?
                ORDER BY timestamp""",
            [metric_ids, window_start, cutoff],
        ).fetchall()
    return rows


def _read_previous_lab(directory: Path, available_cutoff):
    path = directory / "observations.parquet"
    escaped = str(path).replace("'", "''")
    with duckdb.connect(":memory:") as db:
        row = db.execute(
            f"""SELECT timestamp, value, flags
                FROM read_parquet('{escaped}')
                WHERE metric_id=? AND timestamp <= ?
                  AND value IS NOT NULL
                  AND NOT contains(flags, 'invalid')
                  AND NOT contains(flags, 'conflict')
                ORDER BY timestamp DESC LIMIT 1""",
            [TARGET_METRIC, available_cutoff],
        ).fetchone()
    if not row:
        return None
    timestamp, value, flags = row
    return {"timestamp": timestamp, "value": float(value)}


def forecast_sulfur(directory: Path, at: str, artifact_path: Path = ARTIFACT) -> dict[str, Any]:
    """Predict sulphur at ``at`` from only data available four hours earlier."""

    target_time = parse_time(at)
    artifact = _load_artifact(artifact_path)
    lag_minutes = float(artifact.get("availability_lag_minutes", LIMS_PUBLICATION_DELAY_MINUTES))
    lag = timedelta(minutes=lag_minutes)
    cutoff = target_time - lag
    window_start = cutoff - timedelta(hours=6)

    feature_columns = list(artifact["feature_columns"])
    base_ids = sorted({mid for feature in feature_columns if (mid := _base_metric(feature))})
    rows = _read_rows(directory, base_ids, cutoff, window_start)
    observations: dict[str, list[tuple[Any, float]]] = {mid: [] for mid in base_ids}
    for metric_id, timestamp, value, flags in rows:
        flags = str(flags or "")
        if {"invalid", "conflict"} & set(filter(None, flags.split("|"))):
            continue
        if value is None:
            continue
        observations.setdefault(metric_id, []).append((timestamp, float(value)))

    values: list[float] = []
    used_timestamps = []
    imputed = 0
    for feature in feature_columns:
        if feature == "previous_lab_available":
            # Filled below after the as-of laboratory lookup.
            values.append(np.nan)
            continue
        metric_id = _base_metric(feature)
        series = observations.get(metric_id or "", [])
        selected: list[tuple[Any, float]] = []
        if feature.endswith("__mean_1h"):
            begin = cutoff - timedelta(hours=1)
            selected = [(ts, val) for ts, val in series if ts > begin and ts <= cutoff]
            value = float(np.mean([item[1] for item in selected])) if selected else np.nan
        elif feature.endswith("__mean_6h"):
            selected = [(ts, val) for ts, val in series if ts > window_start and ts <= cutoff]
            value = float(np.mean([item[1] for item in selected])) if selected else np.nan
        else:
            selected = [item for item in series if item[0] <= cutoff]
            value = selected[-1][1] if selected else np.nan
        # The offline benchmark used a 30-minute as-of tolerance.  A long
        # telemetry gap is therefore a missing feature, rather than a reason
        # to silently carry an arbitrarily old value into a recommendation.
        if selected and cutoff - selected[-1][0] > FEATURE_TOLERANCE:
            value = np.nan
            selected = []
        if np.isfinite(value):
            used_timestamps.extend(ts for ts, _ in selected)
        else:
            imputed += 1
        values.append(value)

    # The training pipeline exposes a laboratory result when it is available
    # at the feature cutoff (target time minus the publication delay).  Do not
    # subtract the lag a second time here: that would throw away a valid
    # result and make runtime inference differ from the benchmark.
    previous = _read_previous_lab(directory, cutoff)
    previous_value = previous["value"] if previous else None
    try:
        previous_index = feature_columns.index("previous_lab_available")
    except ValueError:
        previous_index = None
    if previous_index is not None:
        values[previous_index] = previous_value if previous_value is not None else np.nan
        if previous_value is None:
            imputed += 1

    raw = np.asarray(values, dtype=float)
    medians = np.asarray(artifact["medians"], dtype=float)
    mean = np.asarray(artifact["mean"], dtype=float)
    scale = np.asarray(artifact["scale"], dtype=float)
    coef = np.asarray(artifact["coef"], dtype=float)
    finite = np.isfinite(raw)
    filled = np.where(finite, raw, medians)
    standardized = (filled - mean) / scale
    log_prediction = float(np.array([1.0, *standardized]) @ coef)
    ridge = max(0.0, float(np.expm1(log_prediction)))
    risk_guard = max(ridge, previous_value) if previous_value is not None else ridge
    feature_time = max(used_timestamps) if used_timestamps else None
    leakage_passed = feature_time is None or feature_time <= cutoff

    warning_values = [
        "Не все признаки доступны; пропуски заменены train-only медианами."
        if imputed
        else None,
        "Доступен риск-контур по предыдущему ЛИМС измерению."
        if previous_value is not None
        else None,
    ]
    return {
        "at": target_time.isoformat(),
        "target": {"metric_id": TARGET_METRIC, "unit": artifact.get("target", {}).get("unit", "мг/кг")},
        "prediction_ridge": ridge,
        "prediction_previous_lab": previous_value,
        "prediction_risk_guard": risk_guard,
        "alarm_above_10": risk_guard > 10,
        "feature_cutoff": cutoff.isoformat(),
        "feature_time": feature_time.isoformat() if feature_time is not None else None,
        "availability_lag_minutes": lag_minutes,
        "feature_count": len(feature_columns),
        "imputed_feature_count": imputed,
        "model": {
            "name": "standardized Ridge on log1p(target)",
            "alpha": artifact.get("selected_alpha", artifact.get("alpha")),
            "artifact": str(artifact_path.name),
            "risk_guard": "max(ridge, available previous lab)",
        },
        "leakage_check": {
            "passed": leakage_passed,
            "feature_time": feature_time.isoformat() if feature_time is not None else None,
            "cutoff": cutoff.isoformat(),
            "violations": 0 if leakage_passed else 1,
        },
        "warnings": [warning for warning in warning_values if warning],
    }
