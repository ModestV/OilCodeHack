"""Runtime adapter for the two-stage sulphur nowcast/forecast (artifact v4, walk-forward).

Independent clocks: the forecast origin (``at``), the forecast horizon and the
LIMS publication delay.  Features are rebuilt from the imported Parquet
dataset with the same code as the offline benchmark
(``tools/modeling/sulfur_features.py``), so offline predictions and runtime
predictions agree for the same origin.  The artifact holds one model set per
``fit_end``; the runtime uses the latest set whose ``fit_end`` is not after
the origin, so a historical origin never sees a model that learned from data
published after it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from tools.modeling.sulfur_features import (
    ANALYSER_METRICS,
    CONTROL_DELTA_COLUMNS,
    CONTROL_METRICS,
    DYNAMICS_COLUMNS,
    FEATURE_COLUMNS,
    LAB_ANCHOR_MAX_AGE_DAYS,
    SUPPORT_COLUMNS,
    applicability,
    build_features,
    clean_analyser,
    exceedance_probability,
    interval,
    ln,
    predict_linear,
    stage2_predict,
    widen_quantiles,
)

from .analytics import parse_time
from .config import ROOT

ARTIFACT = ROOT / "reports" / "modeling" / "sulfur-forecast" / "model.json"
TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
CONTROL_HISTORY_HOURS = 48
# Analyser history must cover the lab-anchor window: offsets are estimated at
# the sampling times of the last published labs, up to 45 days back.
ANALYSER_HISTORY_DAYS = LAB_ANCHOR_MAX_AGE_DAYS + 1
LAB_HISTORY_DAYS = 60
MAX_HORIZON_MINUTES = 180
_ARTIFACT_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


class ForecastUnavailable(ValueError):
    """The model artifact or dataset cannot be used."""


def _validate_artifact(artifact: dict[str, Any]) -> None:
    required = ("walk_forward", "feature_columns", "dynamics_columns", "stage2_inputs", "applicability_policy",
                "horizons_minutes", "lims_publication_delay_minutes", "hard_limit", "model_available_from")
    if artifact.get("version") != 4 or any(key not in artifact for key in required):
        raise ForecastUnavailable("Артефакт модели прогноза серы имеет неизвестный временной контракт")
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    if 0 not in horizons or MAX_HORIZON_MINUTES not in horizons or sorted(horizons) != horizons:
        raise ForecastUnavailable("Артефакт модели должен содержать горизонты 0 и 180 минут по возрастанию")
    if list(artifact["feature_columns"]) != SUPPORT_COLUMNS or list(artifact["dynamics_columns"]) != DYNAMICS_COLUMNS:
        raise ForecastUnavailable("Артефакт модели обучен на другом наборе признаков, чем ожидает runtime")
    entries = artifact["walk_forward"]
    if not entries or [e["fit_end"] for e in entries] != sorted(e["fit_end"] for e in entries):
        raise ForecastUnavailable("Артефакт модели не содержит упорядоченных walk-forward моделей")

    def check_linear(model: dict, columns: list[str], what: str) -> None:
        if list(model.get("feature_columns", [])) != columns:
            raise ForecastUnavailable(f"Артефакт модели содержит несовместимые признаки ({what})")
        size = len(columns)
        for key in ("medians", "mean", "scale", "coef"):
            expected = size + 1 if key == "coef" else size
            if len(model.get(key, [])) != expected or not np.isfinite(np.asarray(model[key], dtype=float)).all():
                raise ForecastUnavailable(f"Артефакт модели содержит несовместимые признаки ({what})")
        if size == 0 or min(model["scale"]) <= 0:
            raise ForecastUnavailable(f"Артефакт модели содержит неверную нормировку ({what})")

    for entry in entries:
        for horizon in horizons:
            key = str(horizon)
            support = entry.get("support", {}).get(key)
            stage2 = entry.get("stage2", {}).get(key)
            if not support or not stage2:
                raise ForecastUnavailable(f"В артефакте нет модели для горизонта {horizon} мин")
            check_linear(support, SUPPORT_COLUMNS, f"support {horizon}")
            if len(support.get("support_lower", [])) != len(SUPPORT_COLUMNS) or len(support.get("support_upper", [])) != len(SUPPORT_COLUMNS):
                raise ForecastUnavailable("Артефакт модели содержит несовместимые границы применимости")
            expected_inputs = artifact["stage2_inputs"]["0" if horizon == 0 else "h"]
            for model_key in ("stage2", "persistence"):
                model = stage2 if model_key == "stage2" else entry.get("persistence", {}).get(key)
                if not model or list(model.get("feature_columns", [])) != list(expected_inputs):
                    raise ForecastUnavailable(f"Артефакт модели содержит несовместимые входы ступени 2 ({horizon} мин)")
                if model.get("kind") == "linear":
                    check_linear(model, list(expected_inputs), f"stage2 {horizon}")
                elif len(model.get("weights", [])) != len(expected_inputs) or min(model["weights"]) < 0 or sum(model["weights"]) <= 0:
                    raise ForecastUnavailable(f"Артефакт модели содержит неверные веса ступени 2 ({horizon} мин)")
            for qkey in ("residual_quantiles", "residual_quantiles_persistence"):
                quantiles = stage2.get(qkey, {})
                if len(quantiles.get("quantiles", [])) < 3 or len(quantiles["quantiles"]) != len(quantiles.get("probabilities", [])):
                    raise ForecastUnavailable("Артефакт модели не содержит квантилей остатков")
            if horizon:
                for name in ANALYSER_METRICS:
                    model = entry.get("stage1", {}).get(name, {}).get(key)
                    if not model:
                        raise ForecastUnavailable(f"В артефакте нет модели динамики {name} для горизонта {horizon} мин")
                    check_linear(model, DYNAMICS_COLUMNS, f"stage1 {name} {horizon}")
                    if model.get("analyser_column") not in DYNAMICS_COLUMNS:
                        raise ForecastUnavailable("Артефакт модели динамики не указывает столбец анализатора")


def _load_artifact(path: Path = ARTIFACT) -> dict[str, Any]:
    """Validated artifact, cached by file identity (path, mtime, size)."""
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = _ARTIFACT_CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]
        content = path.read_bytes()
        artifact = json.loads(content)
    except (OSError, ValueError) as exc:
        raise ForecastUnavailable("Артефакт модели прогноза серы отсутствует или повреждён") from exc
    _validate_artifact(artifact)
    artifact["sha256"] = hashlib.sha256(content).hexdigest()
    _ARTIFACT_CACHE[path] = (key, artifact)
    return artifact


def select_model(artifact: dict[str, Any], origin) -> dict[str, Any] | None:
    """Latest walk-forward model whose ``fit_end`` is not after ``origin`` (None before the first)."""
    origin = pd.Timestamp(origin)
    chosen = None
    for entry in artifact["walk_forward"]:
        if pd.Timestamp(entry["fit_end"]) <= origin:
            chosen = entry
    return chosen


def applicable_model(at, artifact_path: Path = ARTIFACT) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Artifact and the walk-forward entry applicable at ``at`` (a convenience for the scenario layer)."""
    artifact = _load_artifact(artifact_path)
    return artifact, select_model(artifact, parse_time(at))


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


def _stage1(model_entry: dict, horizon: int, features: pd.Series, policy: dict) -> tuple[dict[str, dict], bool]:
    """Anchored Stage-1 estimates per analyser at origin+horizon and whether any fell back."""
    raw = features.loc[DYNAMICS_COLUMNS].to_numpy(dtype=float)
    missing_fraction = float((~np.isfinite(raw)).mean())
    out = {}
    any_fallback = False
    for name in ANALYSER_METRICS:
        model = model_entry["stage1"][name][str(horizon)]
        now = float(features[model["analyser_column"]])
        offset = features.get(f"offset_{name}")
        offset = float(offset) if pd.notna(offset) else np.nan
        if not np.isfinite(now):
            out[name] = {"status": "no_analyser", "delta_ln": None, "predicted_raw": None, "predicted_anchored": None, "fallback": True}
            continue
        delta = float(predict_linear(model, raw))
        fallback = missing_fraction > policy["max_missing_fraction"] or not np.isfinite(delta)
        if fallback:
            delta = 0.0
            any_fallback = True
        predicted_raw = float(np.exp(now + delta))
        anchored = float(predicted_raw + offset) if np.isfinite(offset) else None
        out[name] = {"status": "fallback_persistence" if fallback else "ok", "delta_ln": delta, "predicted_raw": predicted_raw,
                     "predicted_anchored": anchored, "fallback": fallback, "training_filter": model.get("training_filter")}
    return out, any_fallback


def _horizon_estimate(model_entry: dict, horizon: int, features: pd.Series, artifact: dict) -> dict:
    """Stage-2 ln estimate for one fitted horizon plus the residual quantile function to use."""
    key = str(horizon)
    stage2 = model_entry["stage2"][key]
    stage1 = {}
    fallback = False
    if horizon == 0:
        inputs = {"ln_q21": features["ln_q21"], "ln_pak": features["ln_pak"]}
    else:
        stage1, fallback = _stage1(model_entry, horizon, features, artifact["applicability_policy"])
        inputs = {f"ln_{name}_pred": ln(stage1[name]["predicted_anchored"] if stage1[name]["predicted_anchored"] is not None else np.nan)
                  for name in ANALYSER_METRICS}
    inputs["ln_level"] = features["ln_level"]
    inputs["ln_previous_lab"] = features["ln_previous_lab"]
    model = model_entry["persistence"][key] if fallback else stage2
    if fallback:
        for name in ANALYSER_METRICS:
            inputs[f"ln_{name}_pred"] = features[f"ln_{name}"]
    vector = np.array([float(inputs[c]) if pd.notna(inputs[c]) else np.nan for c in model["feature_columns"]], dtype=float)
    ln_pred = float(stage2_predict(model, vector))
    quantiles = stage2["residual_quantiles_persistence"] if fallback else stage2["residual_quantiles"]
    return {"ln_prediction": ln_pred, "quantiles": quantiles, "stage1": stage1, "fallback": fallback,
            "path": "persistence" if fallback else "stage2", "kind": model.get("label") or model.get("kind"),
            "inputs": {c: (float(v) if np.isfinite(v) else None) for c, v in zip(model["feature_columns"], vector)}}


def _blend(a: dict, b: dict, weight: float) -> dict:
    """Residual quantile function at an intermediate horizon (linear between fitted horizons)."""
    if weight <= 0:
        return a
    if weight >= 1:
        return b
    return {"probabilities": a["probabilities"],
            "quantiles": ((1 - weight) * np.asarray(a["quantiles"]) + weight * np.asarray(b["quantiles"])).tolist(),
            "count": min(a["count"], b["count"]),
            "sigma": float((1 - weight) * (a.get("sigma") or 0) + weight * (b.get("sigma") or 0)) or None}


def _bracket(horizons: list[int], horizon: float) -> tuple[int, int, float]:
    lower = max(h for h in horizons if h <= horizon)
    upper = min(h for h in horizons if h >= horizon)
    weight = 0.0 if lower == upper else (horizon - lower) / (upper - lower)
    return lower, upper, weight


def exceedance_at(model_entry: dict, horizon: float, ln_prediction: float, *, extra_sigma: float = 0.0,
                  limit: float | None = None, persistence: bool = False, hard_limit: float = 10.0) -> dict:
    """P(actual > limit) and 80% interval for a (possibly scenario-adjusted) ln prediction.

    ``extra_sigma`` widens the residual distribution by the ln uncertainty of
    a scenario effect; ``persistence`` selects the quantiles of the
    persistence path.
    """
    limit = float(hard_limit) if limit is None else float(limit)
    horizons = sorted(int(h) for h in model_entry["stage2"])
    lower, upper, weight = _bracket(horizons, horizon)
    key = "residual_quantiles_persistence" if persistence else "residual_quantiles"
    quantiles = _blend(model_entry["stage2"][str(lower)][key], model_entry["stage2"][str(upper)][key], weight)
    quantiles = widen_quantiles(quantiles, extra_sigma)
    low, high = interval(quantiles, ln_prediction)
    return {"prediction": float(np.exp(ln_prediction)), "lower": low, "upper": high,
            "exceedance_probability": exceedance_probability(quantiles, ln_prediction, np.log(limit)),
            "limit": limit, "interval": "80% (10-90% квантили out-of-fold остатков)",
            "widened_by_ln_sigma": float(extra_sigma or 0.0)}


def forecast_sulfur(directory: Path, at: str, artifact_path: Path = ARTIFACT, horizon_minutes: float = MAX_HORIZON_MINUTES) -> dict[str, Any]:
    """Issue a nowcast and a forecast for ``horizon_minutes`` at origin ``at``."""
    origin = parse_time(at)
    artifact = _load_artifact(artifact_path)
    if not 0 <= horizon_minutes <= MAX_HORIZON_MINUTES:
        raise ValueError("Горизонт прогноза должен быть в диапазоне 0–180 минут")
    horizon = float(horizon_minutes)
    target_time = origin + timedelta(minutes=horizon)
    hard_limit = float(artifact["hard_limit"])
    analysers, controls, labs = _runtime_inputs(directory, origin, artifact)
    frame = build_features(analysers, controls, labs, pd.DatetimeIndex([pd.Timestamp(origin)]), artifact["lims_publication_delay_minutes"])
    features = frame.iloc[0]
    raw = features.loc[SUPPORT_COLUMNS].to_numpy(dtype=float)
    anchor_pairs = int(max(features.get("anchor_pairs_q21", 0), features.get("anchor_pairs_pak", 0)))
    horizons = [int(h) for h in artifact["horizons_minutes"]]
    model_entry = select_model(artifact, origin)
    model_available_from = artifact.get("model_available_from")

    per_horizon: dict[int, dict] = {}
    if model_entry is None:
        support = {"status": "abstain", "reasons": ["model_not_yet_available_at_origin"], "missing_fraction": float((~np.isfinite(raw)).mean()),
                   "ood_fraction": None, "ood_feature_count": None, "max_absolute_z": None, "policy": artifact["applicability_policy"]}
        for h in horizons:
            per_horizon[h] = {"support": support}
    else:
        for h in horizons:
            support_model = {**model_entry["support"][str(h)], "applicability_policy": artifact["applicability_policy"]}
            gate = applicability(raw, support_model, anchor_pairs)
            entry = {"support": gate}
            if gate["status"] == "ok":
                estimate = _horizon_estimate(model_entry, h, features, artifact)
                if not (np.isfinite(estimate["ln_prediction"]) and estimate["ln_prediction"] < 50):
                    gate["status"] = "abstain"
                    gate["reasons"].append("nonfinite_model_output")
                else:
                    entry.update(estimate)
            per_horizon[h] = entry
    lower_h, upper_h, weight = _bracket(horizons, horizon)
    support = {**per_horizon[upper_h]["support"]}
    support["reasons"] = list(dict.fromkeys(per_horizon[lower_h]["support"]["reasons"] + per_horizon[upper_h]["support"]["reasons"]))
    support["status"] = "ok" if per_horizon[lower_h]["support"]["status"] == "ok" == per_horizon[upper_h]["support"]["status"] else "abstain"

    previous = None
    if pd.notna(features.get("previous_lab_available")):
        previous = {"sample_time": pd.Timestamp(features["previous_lab_sample_time"]).isoformat(),
                    "available_at": pd.Timestamp(features["previous_lab_available_at"]).isoformat(),
                    "value": float(features["previous_lab_available"])}
    latest = max((s.dropna().index.max() for s in list(analysers.values()) + list(controls.values()) if s.notna().any()), default=pd.NaT)
    feature_time = pd.Timestamp(latest).isoformat() if pd.notna(latest) else None
    telemetry_violation = bool(pd.notna(latest) and pd.Timestamp(latest) > pd.Timestamp(origin))
    lab_violation = bool(previous and parse_time(previous["available_at"]) > origin)

    def result_for(h: int) -> dict | None:
        entry = per_horizon[h]
        if entry["support"]["status"] != "ok" or "ln_prediction" not in entry:
            return None
        return exceedance_at(model_entry, h, entry["ln_prediction"], persistence=entry["fallback"], hard_limit=hard_limit)

    nowcast = result_for(0) if per_horizon[0]["support"]["status"] == "ok" else None
    horizon_result = None
    if support["status"] == "ok":
        a, b = per_horizon[lower_h], per_horizon[upper_h]
        ln_pred = (1 - weight) * a["ln_prediction"] + weight * b["ln_prediction"]
        horizon_result = exceedance_at(model_entry, horizon, ln_pred, persistence=a["fallback"] or b["fallback"], hard_limit=hard_limit)
    horizon_rows = []
    for h in horizons:
        entry = per_horizon[h]
        row = {"minutes": h, "status": entry["support"]["status"], "reasons": entry["support"]["reasons"]}
        res = result_for(h)
        if res:
            row.update(res)
            row["stage1"] = entry["stage1"]
            row["path"] = entry["path"]
            row["stage2_kind"] = entry["kind"]
            row["inputs"] = entry["inputs"]
        horizon_rows.append(row)
    nearest = min(horizons, key=lambda h: abs(h - horizon))
    alarm_probability = float(model_entry["stage2"][str(nearest)]["alarm_probability"]) if model_entry else None
    stage1_status = None
    if model_entry and upper_h:
        entry = per_horizon[upper_h]
        if "stage1" in entry:
            stage1_status = "fallback_persistence" if entry["fallback"] else "ok"

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

    in_flight = {c: (float(features[c]) if pd.notna(features[c]) else None) for c in CONTROL_DELTA_COLUMNS}
    warnings = []
    if not np.isfinite(raw).all():
        warnings.append("Пропущенные признаки заменяются train-only медианами только в пределах допустимого покрытия.")
    if support["status"] == "abstain":
        warnings.append("Модель воздержалась от численного прогноза: недостаточно данных или признаки вне области обучения.")
    if stage1_status == "fallback_persistence":
        warnings.append("Динамика анализатора недоступна (неполные входы): путь без воздействия — персистентность с более широкими интервалами.")
    if horizon >= 120:
        warnings.append("На горизонте 2–3 часа суточная проба предсказуема лишь частично; используйте интервал и вероятность превышения.")
    warnings.append("Прогноз — статистическая оценка серы на выходе гидроочистки, не сертификация товарного топлива и не доказательство эффекта управления.")
    return {
        "at": origin.isoformat(), "prediction_origin": origin.isoformat(), "target_time": target_time.isoformat(),
        "horizon_minutes": horizon, "status": support["status"], "reasons": support["reasons"],
        "target": {"metric_id": TARGET_METRIC, "unit": artifact.get("target", {}).get("unit", "мг/кг")},
        "hard_limit": hard_limit,
        "nowcast": nowcast,
        "prediction": horizon_result["prediction"] if horizon_result else None,
        "prediction_lower": horizon_result["lower"] if horizon_result else None,
        "prediction_upper": horizon_result["upper"] if horizon_result else None,
        "exceedance_probability": horizon_result["exceedance_probability"] if horizon_result else None,
        "alarm_probability": alarm_probability,
        "alarm_above_10": (horizon_result["exceedance_probability"] >= alarm_probability or horizon_result["prediction"] > hard_limit) if horizon_result else None,
        "prediction_previous_lab": previous["value"] if previous else None,
        "previous_lab": previous,
        "lab_anchor": {"level": float(np.exp(features["ln_level"])) if pd.notna(features["ln_level"]) else None,
                       "samples": int(artifact.get("lab_anchor_samples", 0)), "pairs": anchor_pairs},
        "analysers": {name: analyser_evidence(name) for name in ANALYSER_METRICS},
        "horizons": horizon_rows,
        "stage1": {"status": stage1_status, "horizons": {str(h): per_horizon[h].get("stage1") for h in horizons if h and "stage1" in per_horizon[h]}},
        "in_flight_controls": in_flight,
        "feature_cutoff": origin.isoformat(), "feature_time": feature_time,
        "model_available_from": model_available_from,
        "lims_publication_delay_minutes": float(artifact["lims_publication_delay_minutes"]),
        "feature_count": len(raw), "imputed_feature_count": int((~np.isfinite(raw)).sum()), "applicability": support,
        "model": {"name": artifact.get("model"), "horizons_minutes": horizons, "artifact": artifact_path.name,
                  "artifact_sha256": artifact["sha256"], "fit_end": model_entry["fit_end"] if model_entry else None,
                  "available_from": model_entry["fit_end"] if model_entry else model_available_from,
                  "stage2_kind_by_horizon": {h: (model_entry["stage2"][str(h)].get("label") or model_entry["stage2"][str(h)].get("kind")) for h in horizons} if model_entry else None,
                  "feature_columns": FEATURE_COLUMNS},
        "leakage_check": {"passed": not (telemetry_violation or lab_violation), "feature_time": feature_time,
                          "cutoff": origin.isoformat(), "previous_lab_available_at": previous["available_at"] if previous else None,
                          "violations": int(telemetry_violation) + int(lab_violation)},
        "warnings": warnings,
    }
