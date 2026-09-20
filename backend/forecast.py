"""Runtime adapter with independent forecast-origin and LIMS-publication clocks."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from tools.modeling.sulfur_features import applicability, available_lab, telemetry_features
from .analytics import parse_time
from .config import ROOT

ARTIFACT = ROOT / "reports" / "modeling" / "sulfur-first-iteration" / "model.json"
TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
TELEMETRY_STAGE_MAP = {"242000": "ht", "24-2000": "ht"}


class ForecastUnavailable(ValueError):
    """The model artifact or dataset cannot be used."""


def _load_artifact(path: Path = ARTIFACT) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        artifact = json.loads(content)
    except (OSError, ValueError) as exc:
        raise ForecastUnavailable("Артефакт модели прогноза серы отсутствует или повреждён") from exc
    required = ("feature_columns", "medians", "mean", "scale", "coef", "support_lower", "support_upper",
                "applicability_policy", "forecast_horizon_minutes", "lims_publication_delay_minutes")
    if artifact.get("version") != 2 or any(key not in artifact for key in required):
        raise ForecastUnavailable("Артефакт модели прогноза серы имеет неизвестный временной контракт")
    size = len(artifact["feature_columns"])
    for key in ("medians", "mean", "scale", "support_lower", "support_upper", "coef"):
        expected = size + 1 if key == "coef" else size
        if len(artifact[key]) != expected or not np.isfinite(np.asarray(artifact[key], dtype=float)).all():
            raise ForecastUnavailable("Артефакт модели содержит несовместимые признаки")
    if size == 0 or min(artifact["scale"]) <= 0:
        raise ForecastUnavailable("Артефакт модели содержит неверную нормализацию")
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    return artifact


def _base_feature(feature: str) -> str | None:
    if feature == "previous_lab_available":
        return None
    for suffix in ("__mean_1h", "__mean_6h"):
        if feature.endswith(suffix):
            return feature[:-len(suffix)]
    return feature


def _base_metric(feature: str) -> str | None:
    base = _base_feature(feature)
    if base is None or "__" not in base:
        return None
    stage, code = base.split("__", 1)
    return f"{TELEMETRY_STAGE_MAP.get(stage, stage)}.{code}"


def _runtime_features(directory: Path, origin, artifact: dict) -> tuple[np.ndarray, Any, dict | None]:
    path = directory / "observations.parquet"
    if not path.exists():
        raise ForecastUnavailable("В наборе нет импортированных наблюдений")
    base_columns = list(dict.fromkeys(base for f in artifact["feature_columns"] if (base := _base_feature(f))))
    metric_to_column = {_base_metric(c): c for c in base_columns}
    escaped = str(path).replace("'", "''")
    publication_delay = float(artifact["lims_publication_delay_minutes"])
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"""SELECT metric_id, timestamp, value FROM read_parquet('{escaped}')
                WHERE source='kip' AND metric_id IN (SELECT unnest(?))
                  AND timestamp > ? AND timestamp <= ?
                  AND value IS NOT NULL AND isfinite(value)
                  AND NOT contains(coalesce(flags, ''), 'invalid')
                  AND NOT contains(coalesce(flags, ''), 'conflict')
                ORDER BY timestamp""",
            [list(metric_to_column), origin - timedelta(hours=6), origin],
        ).fetchdf()
        labs = db.execute(
            f"""SELECT timestamp AS target_time, value AS target FROM read_parquet('{escaped}')
                WHERE source='lims' AND metric_id=? AND timestamp<=?
                  AND value IS NOT NULL AND isfinite(value) AND value>=0
                  AND NOT contains(coalesce(flags, ''), 'invalid')
                  AND NOT contains(coalesce(flags, ''), 'conflict') ORDER BY timestamp""",
            [TARGET_METRIC, origin - timedelta(minutes=publication_delay)],
        ).fetchdf()
    if rows.empty:
        telemetry = pd.DataFrame(index=pd.DatetimeIndex([]), columns=base_columns, dtype=float)
    else:
        rows["column"] = rows["metric_id"].map(metric_to_column)
        telemetry = rows.pivot_table(index="timestamp", columns="column", values="value", aggfunc="last")
        telemetry = telemetry.reindex(columns=base_columns)
    origins = pd.DatetimeIndex([origin])
    frame, latest = telemetry_features(telemetry, origins)
    lab = available_lab(labs, origins, publication_delay)
    frame["previous_lab_available"] = lab["previous_lab_available"]
    previous = None
    if pd.notna(lab.loc[0, "previous_lab_available"]):
        previous = {"sample_time": lab.loc[0, "previous_lab_sample_time"].isoformat(),
                    "available_at": lab.loc[0, "previous_lab_available_at"].isoformat(),
                    "value": float(lab.loc[0, "previous_lab_available"])}
    return frame.loc[0, artifact["feature_columns"]].to_numpy(dtype=float), latest.iloc[0], previous


def forecast_sulfur(directory: Path, at: str, artifact_path: Path = ARTIFACT) -> dict[str, Any]:
    """Issue a forecast at ``at`` (origin), for the artifact's fixed future horizon."""
    origin = parse_time(at)
    artifact = _load_artifact(artifact_path)
    horizon = float(artifact["forecast_horizon_minutes"])
    target_time = origin + timedelta(minutes=horizon)
    raw, latest, previous = _runtime_features(directory, origin, artifact)
    support = applicability(raw, artifact)
    model_available_from = artifact.get("model_available_from")
    if model_available_from and origin < parse_time(model_available_from):
        support["status"] = "abstain"
        support["reasons"].append("model_not_yet_available_at_origin")
    finite = np.isfinite(raw)
    previous_value = previous["value"] if previous else None
    ridge = risk_guard = None
    if support["status"] == "ok":
        filled = np.where(finite, raw, np.asarray(artifact["medians"]))
        standardized = (filled - np.asarray(artifact["mean"])) / np.asarray(artifact["scale"])
        log_prediction = float(np.array([1.0, *standardized]) @ np.asarray(artifact["coef"]))
        if not np.isfinite(log_prediction) or log_prediction > 700:
            support["status"] = "abstain"
            support["reasons"].append("nonfinite_model_output")
        else:
            ridge = max(0.0, float(np.expm1(log_prediction)))
            risk_guard = max(ridge, previous_value) if previous_value is not None else ridge
    feature_time = latest.isoformat() if pd.notna(latest) else None
    telemetry_violation = bool(pd.notna(latest) and latest > origin)
    lab_violation = bool(previous and parse_time(previous["available_at"]) > origin)
    warnings = []
    if not finite.all():
        warnings.append("Пропущенные признаки заменяются train-only медианами только в пределах допустимого покрытия.")
    if support["status"] == "abstain":
        warnings.append("Модель воздержалась от численного прогноза: недостаточно данных или признаки вне области обучения.")
    warnings.append("Пороговый risk_guard — эвристика; точность обнаружения превышений ограничена. Прогноз не доказывает эффект управления.")
    return {
        "at": origin.isoformat(), "prediction_origin": origin.isoformat(), "target_time": target_time.isoformat(),
        "status": support["status"], "reasons": support["reasons"],
        "target": {"metric_id": TARGET_METRIC, "unit": artifact.get("target", {}).get("unit", "мг/кг")},
        "prediction_ridge": ridge, "prediction_previous_lab": previous_value,
        "prediction_risk_guard": risk_guard, "alarm_above_10": risk_guard > 10 if risk_guard is not None else None,
        "previous_lab": previous,
        "feature_cutoff": origin.isoformat(), "feature_time": feature_time,
        "forecast_horizon_minutes": horizon,
        "model_available_from": model_available_from,
        "lims_publication_delay_minutes": float(artifact["lims_publication_delay_minutes"]),
        "feature_count": len(raw), "imputed_feature_count": int((~finite).sum()), "applicability": support,
        "model": {"name": "standardized Ridge on log1p(target)", "alpha": artifact.get("selected_alpha", artifact.get("alpha")),
                  "artifact": artifact_path.name, "artifact_sha256": artifact["sha256"],
                  "risk_guard": "max(ridge, lab result published by origin)"},
        "leakage_check": {"passed": not (telemetry_violation or lab_violation), "feature_time": feature_time,
                          "cutoff": origin.isoformat(), "previous_lab_available_at": previous["available_at"] if previous else None,
                          "violations": int(telemetry_violation) + int(lab_violation)},
        "warnings": warnings,
    }
