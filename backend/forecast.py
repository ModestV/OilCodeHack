"""Runtime adapter for the lab-anchored sulphur nowcast/forecast (artifact v3).

Independent clocks: the forecast origin (``at``), the forecast horizon and the
LIMS publication delay.  Features are rebuilt from the imported Parquet
dataset with the same code as the offline benchmark
(``tools/modeling/anchored_features.py``), so offline predictions and runtime
predictions agree for the same origin.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from tools.modeling.anchored_features import (
    ANALYSER_METRICS,
    CONTROL_METRICS,
    FEATURE_COLUMNS,
    LAB_ANCHOR_MAX_AGE_DAYS,
    applicability,
    build_features,
    clean_analyser,
    exceedance_probability,
    interval,
    predict_portable,
)

from .analytics import parse_time
from .config import ROOT

ARTIFACT = ROOT / "reports" / "modeling" / "causal-anchored" / "corrected-claude" / "model.json"
TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
CONTROL_HISTORY_HOURS = 48
# Analyser history must cover the lab-anchor window: offsets are estimated at
# the sampling times of the last published labs, up to 45 days back.
ANALYSER_HISTORY_DAYS = LAB_ANCHOR_MAX_AGE_DAYS + 1
LAB_HISTORY_DAYS = 60
MAX_HORIZON_MINUTES = 180
# Opt-in shadow model: the repaired two-stage v4r is served only when this
# variable equals TWO_STAGE_MODEL; v3 stays the default (decision 2026-09-22).
FORECAST_MODEL_ENV = "OILCODE_FORECAST_MODEL"
TWO_STAGE_MODEL = "two-stage-v4r"


class ForecastUnavailable(ValueError):
    """The model artifact or dataset cannot be used."""


def _load_artifact(path: Path = ARTIFACT) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        artifact = json.loads(content)
    except (OSError, ValueError) as exc:
        raise ForecastUnavailable("Артефакт модели прогноза серы отсутствует или повреждён") from exc
    required = ("models", "model_columns", "feature_columns", "applicability_policy", "horizons_minutes",
                "lims_publication_delay_minutes", "hard_limit")
    if artifact.get("version") != 3 or any(key not in artifact for key in required):
        raise ForecastUnavailable("Артефакт модели прогноза серы имеет неизвестный временной контракт")
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    if 0 not in horizons or MAX_HORIZON_MINUTES not in horizons or sorted(horizons) != horizons:
        raise ForecastUnavailable("Артефакт модели должен содержать горизонты 0 и 180 минут по возрастанию")
    for horizon in horizons:
        model = artifact["models"].get(str(horizon))
        if not model:
            raise ForecastUnavailable(f"В артефакте нет модели для горизонта {horizon} мин")
        if model.get("input_transform", "identity") not in {"identity", "exp"} or model.get("output_transform", "identity") not in {"identity", "log"}:
            raise ForecastUnavailable("Артефакт содержит неизвестное преобразование модели")
        size = len(model["feature_columns"])
        for key in ("medians", "mean", "scale", "coef"):
            expected = size + 1 if key == "coef" else size
            if len(model[key]) != expected or not np.isfinite(np.asarray(model[key], dtype=float)).all():
                raise ForecastUnavailable("Артефакт модели содержит несовместимые признаки")
        if size == 0 or min(model["scale"]) <= 0 or min(model["support"]["scale"]) <= 0:
            raise ForecastUnavailable("Артефакт модели содержит неверную нормализацию")
        quantiles = model.get("residual_quantiles", {})
        if len(quantiles.get("quantiles", [])) < 3 or len(quantiles["quantiles"]) != len(quantiles.get("probabilities", [])):
            raise ForecastUnavailable("Артефакт модели не содержит квантилей остатков")
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    return artifact


def _runtime_inputs(directory: Path, origin, artifact: dict) -> tuple[dict, dict, pd.DataFrame]:
    path = directory / "observations.parquet"
    if not path.exists():
        raise ForecastUnavailable("В наборе нет импортированных наблюдений")
    escaped = str(path).replace("'", "''")
    publication_delay = float(artifact["lims_publication_delay_minutes"])
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"""SELECT metric_id, timestamp, value, coalesce(flags, '') AS flags FROM read_parquet('{escaped}')
                WHERE ((metric_id IN (SELECT unnest(?)) AND timestamp > ?)
                       OR (metric_id IN (SELECT unnest(?)) AND timestamp > ?))
                  AND timestamp <= ? AND value IS NOT NULL AND isfinite(value)
                  AND NOT contains(coalesce(flags, ''), 'invalid') AND NOT contains(coalesce(flags, ''), 'conflict')
                ORDER BY timestamp""",
            [list(ANALYSER_METRICS.values()), origin - timedelta(days=ANALYSER_HISTORY_DAYS),
             list(CONTROL_METRICS.values()), origin - timedelta(hours=CONTROL_HISTORY_HOURS), origin],
        ).fetchdf()
        labs = db.execute(
            f"""SELECT timestamp AS target_time, value AS target FROM read_parquet('{escaped}')
                WHERE source='lims' AND metric_id=? AND timestamp<=? AND timestamp>=?
                  AND value IS NOT NULL AND isfinite(value) AND value>=0
                  AND NOT contains(coalesce(flags, ''), 'invalid') AND NOT contains(coalesce(flags, ''), 'conflict')
                ORDER BY timestamp""",
            [TARGET_METRIC, origin - timedelta(minutes=publication_delay), origin - timedelta(days=LAB_HISTORY_DAYS)],
        ).fetchdf()
    labs["target_time"] = pd.to_datetime(labs["target_time"]).astype("datetime64[ns]")
    rows["timestamp"] = pd.to_datetime(rows["timestamp"]).astype("datetime64[ns]")
    groups = {mid: part.drop_duplicates("timestamp", keep="last").set_index("timestamp") for mid, part in rows.groupby("metric_id")}
    analysers = {name: clean_analyser(groups[mid]["value"], groups[mid]["flags"]) for name, mid in ANALYSER_METRICS.items() if mid in groups}
    controls = {name: groups[mid]["value"].astype(float) for name, mid in CONTROL_METRICS.items() if mid in groups}
    return analysers, controls, labs


def _predict(model: dict, features: pd.Series) -> float:
    raw = features.loc[model["feature_columns"]].to_numpy(dtype=float)
    return predict_portable(model, raw)


def horizon_support(artifact: dict, raw: np.ndarray, anchor_pairs: int, horizon: float) -> dict:
    """Exact horizon uses its own support; interpolation needs both neighbours."""
    horizons = artifact["horizons_minutes"]
    neighbours = sorted({max(h for h in horizons if h <= horizon), min(h for h in horizons if h >= horizon)})
    checks = {str(h): applicability(raw, {**artifact["models"][str(h)]["support"],
                       "feature_columns": FEATURE_COLUMNS, "applicability_policy": artifact["applicability_policy"]},
                       anchor_pairs) for h in neighbours}
    result = dict(checks[str(neighbours[0])])
    result["reasons"] = list(dict.fromkeys(r for s in checks.values() for r in s["reasons"]))
    result["status"] = "abstain" if result["reasons"] else "ok"
    result["checked_horizons"] = checks
    return result


def _blend_quantiles(artifact: dict, horizon: float) -> dict:
    """Residual quantile function at an arbitrary horizon (linear between fitted horizons)."""
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    lower = max(h for h in horizons if h <= horizon)
    upper = min(h for h in horizons if h >= horizon)
    a = artifact["models"][str(lower)]["residual_quantiles"]
    if lower == upper:
        return a
    b = artifact["models"][str(upper)]["residual_quantiles"]
    weight = (horizon - lower) / (upper - lower)
    return {"probabilities": a["probabilities"],
            "quantiles": ((1 - weight) * np.asarray(a["quantiles"]) + weight * np.asarray(b["quantiles"])).tolist(),
            "count": min(a["count"], b["count"])}


def _ln_prediction_at(ln_by_horizon: dict[int, float], horizon: float) -> float:
    horizons = sorted(ln_by_horizon)
    lower = max(h for h in horizons if h <= horizon)
    upper = min(h for h in horizons if h >= horizon)
    if lower == upper:
        return ln_by_horizon[lower]
    weight = (horizon - lower) / (upper - lower)
    return (1 - weight) * ln_by_horizon[lower] + weight * ln_by_horizon[upper]


def _alarm_probability(artifact: dict, horizon: float) -> float:
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    nearest = min(horizons, key=lambda h: abs(h - horizon))
    return float(artifact["models"][str(nearest)]["alarm_probability"])


def exceedance_at(artifact: dict, horizon: float, ln_prediction: float, limit: float | None = None) -> dict:
    """P(actual > limit) and 80% interval for a (possibly scenario-adjusted) ln prediction."""
    limit = float(artifact["hard_limit"]) if limit is None else float(limit)
    quantiles = _blend_quantiles(artifact, horizon)
    low, high = interval(quantiles, ln_prediction)
    return {"prediction": float(np.exp(ln_prediction)), "lower": low, "upper": high,
            "exceedance_probability": exceedance_probability(quantiles, ln_prediction, np.log(limit)),
            "limit": limit, "interval": "80% (10-90% квантили out-of-fold остатков)"}


def selected_model() -> str:
    return TWO_STAGE_MODEL if os.environ.get(FORECAST_MODEL_ENV, "").strip() == TWO_STAGE_MODEL else "v3"


def forecast_sulfur(directory: Path, at: str, artifact_path: Path | None = None,
                    horizon_minutes: float = MAX_HORIZON_MINUTES) -> dict[str, Any]:
    """Nowcast and forecast at ``at``: v3 by default, the two-stage v4r adapter only when opted in."""
    if selected_model() == TWO_STAGE_MODEL:
        from . import forecast_two_stage
        return forecast_two_stage.forecast_sulfur(directory, at, artifact_path or forecast_two_stage.ARTIFACT, horizon_minutes)
    return forecast_sulfur_v3(directory, at, artifact_path or ARTIFACT, horizon_minutes)


def forecast_sulfur_v3(directory: Path, at: str, artifact_path: Path = ARTIFACT, horizon_minutes: float = MAX_HORIZON_MINUTES) -> dict[str, Any]:
    """Issue a nowcast and a forecast for ``horizon_minutes`` at origin ``at``."""
    origin = parse_time(at)
    artifact = _load_artifact(artifact_path)
    if not 0 <= horizon_minutes <= MAX_HORIZON_MINUTES:
        raise ValueError("Горизонт прогноза должен быть в диапазоне 0–180 минут")
    horizon = float(horizon_minutes)
    target_time = origin + timedelta(minutes=horizon)
    analysers, controls, labs = _runtime_inputs(directory, origin, artifact)
    frame = build_features(analysers, controls, labs, pd.DatetimeIndex([pd.Timestamp(origin)]), artifact["lims_publication_delay_minutes"],
                           anchor_samples=int(artifact.get("lab_anchor_samples", 10)),
                           level_samples=int(artifact.get("lab_level_samples", 5)))
    features = frame.iloc[0]
    raw = features.loc[FEATURE_COLUMNS].to_numpy(dtype=float)
    anchor_pairs = int(max(features.get("anchor_pairs_q21", 0), features.get("anchor_pairs_pak", 0)))
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    supports = {h: horizon_support(artifact, raw, anchor_pairs, h) for h in horizons}
    support = horizon_support(artifact, raw, anchor_pairs, horizon)
    model_available_from = artifact.get("model_available_from")
    if model_available_from and origin < parse_time(model_available_from):
        for check in [support, *supports.values()]:
            check["status"] = "abstain"
            check["reasons"].append("model_not_yet_available_at_origin")
    previous = None
    if pd.notna(features.get("previous_lab_available")):
        previous = {"sample_time": pd.Timestamp(features["previous_lab_sample_time"]).isoformat(),
                    "available_at": pd.Timestamp(features["previous_lab_available_at"]).isoformat(),
                    "value": float(features["previous_lab_available"])}
    latest = max((s.dropna().index.max() for s in list(analysers.values()) + list(controls.values()) if s.notna().any()), default=pd.NaT)
    feature_time = pd.Timestamp(latest).isoformat() if pd.notna(latest) else None
    telemetry_violation = bool(pd.notna(latest) and pd.Timestamp(latest) > pd.Timestamp(origin))
    lab_violation = bool(previous and parse_time(previous["available_at"]) > origin)

    ln_by_horizon: dict[int, float] = {}
    nowcast = horizon_result = None
    horizon_rows = []
    if support["status"] == "ok":
        for h in horizons:
            ln_by_horizon[h] = _predict(artifact["models"][str(h)], features)
        if not all(np.isfinite(v) and v < 50 for v in ln_by_horizon.values()):
            support["status"] = "abstain"
            support["reasons"].append("nonfinite_model_output")
    if support["status"] == "ok":
        if supports[0]["status"] == "ok":
            nowcast = exceedance_at(artifact, 0, ln_by_horizon[0])
        horizon_result = exceedance_at(artifact, horizon, _ln_prediction_at(ln_by_horizon, horizon))
        horizon_rows = [{"minutes": h, "status": supports[h]["status"], "reasons": supports[h]["reasons"],
                         **(exceedance_at(artifact, h, ln_by_horizon[h]) if supports[h]["status"] == "ok"
                            else {"prediction": None, "lower": None, "upper": None, "exceedance_probability": None})}
                        for h in horizons]
    path_end = min(h for h in horizons if h >= horizon)
    path_supported = support["status"] == "ok" and all(supports[h]["status"] == "ok" for h in horizons if h <= path_end)
    alarm_probability = _alarm_probability(artifact, horizon)

    def analyser_evidence(name: str) -> dict:
        series = analysers.get(name)
        if series is None or not series.notna().any():
            return {"metric_id": ANALYSER_METRICS[name], "value": None, "timestamp": None, "adjusted": None, "offset": None}
        valid = series.dropna()
        offset = features.get(f"offset_{name}")
        value = float(valid.iloc[-1])
        return {"metric_id": ANALYSER_METRICS[name], "value": value, "timestamp": pd.Timestamp(valid.index[-1]).isoformat(),
                "age_minutes": float((pd.Timestamp(origin) - valid.index[-1]).total_seconds() / 60),
                "offset": float(offset) if pd.notna(offset) else None,
                "adjusted": float(value + offset) if pd.notna(offset) else None,
                "anchor_pairs": int(features.get(f"anchor_pairs_{name}", 0))}

    warnings = []
    if not np.isfinite(raw).all():
        warnings.append("Пропущенные признаки заменяются train-only медианами только в пределах допустимого покрытия.")
    if support["status"] == "abstain":
        warnings.append("Модель воздержалась от численного прогноза: недостаточно данных или признаки вне области обучения.")
    if horizon >= 120:
        warnings.append("На горизонте 2–3 часа точечный прогноз близок к локальному уровню; используйте интервал и вероятность превышения.")
    warnings.append("Прогноз — статистическая оценка серы на выходе гидроочистки, не сертификация товарного топлива и не доказательство эффекта управления.")
    return {
        "at": origin.isoformat(), "prediction_origin": origin.isoformat(), "target_time": target_time.isoformat(),
        "horizon_minutes": horizon, "status": support["status"], "reasons": support["reasons"],
        "target": {"metric_id": TARGET_METRIC, "unit": artifact.get("target", {}).get("unit", "мг/кг")},
        "hard_limit": float(artifact["hard_limit"]),
        "nowcast": nowcast,
        "nowcast_applicability": supports[0], "path_supported": path_supported,
        "prediction": horizon_result["prediction"] if horizon_result else None,
        "prediction_lower": horizon_result["lower"] if horizon_result else None,
        "prediction_upper": horizon_result["upper"] if horizon_result else None,
        "exceedance_probability": horizon_result["exceedance_probability"] if horizon_result else None,
        "alarm_probability": alarm_probability,
        "alarm_above_10": (horizon_result["exceedance_probability"] >= alarm_probability or horizon_result["prediction"] > artifact["hard_limit"]) if horizon_result else None,
        # ln-residual quantile function at the horizon: P(S > limit) for a shifted scenario endpoint.
        "risk_quantiles": ({"space": "ln_residual", **{k: _blend_quantiles(artifact, horizon)[k] for k in ("probabilities", "quantiles")}}
                           if horizon_result else None),
        "prediction_previous_lab": previous["value"] if previous else None,
        "previous_lab": previous,
        "lab_anchor": {"level": float(np.exp(features["ln_level"])) if pd.notna(features["ln_level"]) else None,
                       "samples": int(artifact.get("lab_anchor_samples", 0)), "pairs": anchor_pairs},
        "analysers": {name: analyser_evidence(name) for name in ANALYSER_METRICS},
        "horizons": horizon_rows,
        "feature_cutoff": origin.isoformat(), "feature_time": feature_time,
        "model_available_from": model_available_from,
        "lims_publication_delay_minutes": float(artifact["lims_publication_delay_minutes"]),
        "feature_count": len(raw), "imputed_feature_count": int((~np.isfinite(raw)).sum()), "applicability": support,
        "model": {"name": artifact.get("model"), "horizons_minutes": horizons, "artifact": artifact_path.name,
                  "artifact_sha256": artifact["sha256"], "model_columns": artifact["model_columns"],
                  "alpha": {h: artifact["models"][str(h)]["alpha"] for h in horizons}},
        "leakage_check": {"passed": not (telemetry_violation or lab_violation), "feature_time": feature_time,
                          "cutoff": origin.isoformat(), "previous_lab_available_at": previous["available_at"] if previous else None,
                          "violations": int(telemetry_violation) + int(lab_violation)},
        "warnings": warnings,
    }


def control_response_defaults(artifact_path: Path = ARTIFACT) -> dict[str, Any] | None:
    """Scenario defaults estimated from analyser step responses, or None if unavailable."""
    try:
        artifact = _load_artifact(artifact_path)
    except ForecastUnavailable:
        return None
    response = artifact.get("control_response") or {}
    if not all(response.get(k, {}).get("coefficient") is not None for k in ("T6", "F9", "P13")):
        return None
    return {"temperature_effect": float(response["T6"]["coefficient"]),
            "feed_rate_effect": float(response["F9"]["coefficient"]),
            "pressure_effect": float(response["P13"]["coefficient"]),
            "lag_minutes": int(max(response[k].get("lag_minutes") or 60 for k in ("T6", "F9", "P13"))),
            "events": {k: int(response[k].get("events", 0)) for k in ("T6", "F9", "P13")},
            "basis": "median lagged ln-sulphur response to single-control step events in the online analyser (observational)",
            "artifact_sha256": artifact["sha256"]}
