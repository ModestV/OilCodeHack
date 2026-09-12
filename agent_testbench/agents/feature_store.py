from __future__ import annotations

import math
import sqlite3
from functools import lru_cache
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "databases" / "oilcode_agent_test.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _mad(values: list[float]) -> float:
    if not values:
        return 0.0
    center = median(values)
    return median([abs(value - center) for value in values])


@lru_cache(maxsize=64)
def numeric_series(table: str, column: str) -> tuple[float, ...]:
    with _connect() as conn:
        rows = conn.execute(f'select "{column}" from "{table}"').fetchall()
    values = [_clean_number(row[0]) for row in rows]
    return tuple(value for value in values if value is not None)


def robust_profile(table: str, column: str) -> dict[str, Any]:
    values = list(numeric_series(table, column))
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "std": None,
            "mad": None,
            "p05": None,
            "p25": None,
            "p75": None,
            "p95": None,
            "min": None,
            "max": None,
        }

    return {
        "count": len(values),
        "mean": round(mean(values), 6),
        "median": round(median(values), 6),
        "std": round(pstdev(values), 6) if len(values) > 1 else 0.0,
        "mad": round(_mad(values), 6),
        "p05": round(_percentile(values, 0.05), 6),
        "p25": round(_percentile(values, 0.25), 6),
        "p75": round(_percentile(values, 0.75), 6),
        "p95": round(_percentile(values, 0.95), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def robust_z(table: str, column: str, value: float | None) -> float | None:
    if value is None:
        return None
    profile = robust_profile(table, column)
    if not profile["count"]:
        return None
    mad = profile["mad"] or 0.0
    if mad == 0:
        std = profile["std"] or 0.0
        if std == 0:
            return 0.0
        return round((value - profile["mean"]) / std, 3)
    return round(0.6745 * (value - profile["median"]) / mad, 3)


def recent_rows(table: str, time_col: str, timestamp: str, limit: int = 12) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f'''
            select * from "{table}"
            where "{time_col}" <= ?
            order by "{time_col}" desc
            limit ?
            ''',
            (timestamp, limit),
        ).fetchall()

        if not rows:
            rows = conn.execute(
                f'select * from "{table}" order by "{time_col}" desc limit ?',
                (limit,),
            ).fetchall()

    return [dict(row) for row in reversed(rows)]


def flatline_score(table: str, time_col: str, column: str, timestamp: str, limit: int = 8) -> dict[str, Any]:
    rows = recent_rows(table, time_col, timestamp, limit)
    values = [_clean_number(row.get(column)) for row in rows]
    values = [value for value in values if value is not None]
    if len(values) < 4:
        return {"is_flatline": False, "run_length": len(values), "std": None}
    std = pstdev(values)
    rounded = [round(value, 6) for value in values]
    return {
        "is_flatline": len(set(rounded)) == 1 or std < 1e-9,
        "run_length": len(values),
        "std": round(std, 9),
    }


def trend(table: str, time_col: str, column: str, timestamp: str, limit: int = 12) -> dict[str, Any]:
    rows = recent_rows(table, time_col, timestamp, limit)
    values = [_clean_number(row.get(column)) for row in rows]
    values = [value for value in values if value is not None]
    if len(values) < 2:
        return {"delta": None, "slope_per_step": None, "points": len(values)}
    xs = list(range(len(values)))
    x_mean = mean(xs)
    y_mean = mean(values)
    denom = sum((x - x_mean) ** 2 for x in xs)
    slope = 0.0 if denom == 0 else sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denom
    return {
        "delta": round(values[-1] - values[0], 6),
        "slope_per_step": round(slope, 6),
        "points": len(values),
    }


@lru_cache(maxsize=1)
def train_sulfur_model() -> dict[str, Any]:
    feature_cols = ["T5", "T6", "P13", "F9", "F15", "F26"]
    with _connect() as conn:
        rows = conn.execute(
            '''
            select u.T5, u.T6, u.P13, u.F9, u.F15, u.F26, p.value as sulfur
            from unit_242000_telemetry u
            join pak_measurements p on p.timestamp = u.date
            where p.tag = '24-2000:Mg.Sulfur'
            order by u.date
            '''
        ).fetchall()

    samples = []
    for row in rows:
        features = [_clean_number(row[col]) for col in feature_cols]
        target = _clean_number(row["sulfur"])
        if target is not None and all(value is not None for value in features):
            samples.append((features, target))

    if len(samples) < len(feature_cols) + 2:
        return {
            "status": "insufficient_data",
            "features": feature_cols,
            "sample_count": len(samples),
            "intercept": None,
            "coefficients": {},
            "r2": None,
            "rmse": None,
        }

    try:
        import numpy as np
    except ImportError:
        return _train_univariate_fallback(samples, feature_cols)

    x = np.array([item[0] for item in samples], dtype=float)
    y = np.array([item[1] for item in samples], dtype=float)
    means = x.mean(axis=0)
    stds = x.std(axis=0)
    stds[stds == 0] = 1.0
    x_scaled = (x - means) / stds
    design = np.column_stack([np.ones(len(x_scaled)), x_scaled])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    predictions = design @ beta
    residuals = y - predictions
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    rmse = float(np.sqrt(np.mean(residuals**2)))

    coefficients = {
        feature: round(float(coef / std), 8)
        for feature, coef, std in zip(feature_cols, beta[1:], stds)
    }
    return {
        "status": "trained",
        "features": feature_cols,
        "sample_count": len(samples),
        "intercept": round(float(beta[0] - sum((beta[i + 1] * means[i]) / stds[i] for i in range(len(feature_cols)))), 8),
        "coefficients": coefficients,
        "standardized_coefficients": {
            feature: round(float(coef), 6)
            for feature, coef in zip(feature_cols, beta[1:])
        },
        "r2": round(float(r2), 4),
        "rmse": round(rmse, 4),
        "target": "24-2000:Mg.Sulfur",
    }


def _train_univariate_fallback(samples: list[tuple[list[float], float]], feature_cols: list[str]) -> dict[str, Any]:
    best = None
    for idx, feature in enumerate(feature_cols):
        xs = [sample[0][idx] for sample in samples]
        ys = [sample[1] for sample in samples]
        x_mean = mean(xs)
        y_mean = mean(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom == 0:
            continue
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
        intercept = y_mean - slope * x_mean
        preds = [intercept + slope * x for x in xs]
        ss_res = sum((y - pred) ** 2 for y, pred in zip(ys, preds))
        ss_tot = sum((y - y_mean) ** 2 for y in ys)
        r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
        if best is None or r2 > best["r2"]:
            best = {"feature": feature, "slope": slope, "intercept": intercept, "r2": r2, "preds": preds}

    if best is None:
        return {"status": "insufficient_data", "features": feature_cols, "sample_count": len(samples)}

    rmse = math.sqrt(mean([(y - pred) ** 2 for (_, y), pred in zip(samples, best["preds"])]))
    return {
        "status": "trained_fallback",
        "features": [best["feature"]],
        "sample_count": len(samples),
        "intercept": round(best["intercept"], 8),
        "coefficients": {best["feature"]: round(best["slope"], 8)},
        "r2": round(best["r2"], 4),
        "rmse": round(rmse, 4),
        "target": "24-2000:Mg.Sulfur",
    }


def predict_linear(model: dict[str, Any], features: dict[str, float | None]) -> float | None:
    if not model.get("intercept") and model.get("intercept") != 0:
        return None
    prediction = float(model["intercept"])
    for feature, coef in model.get("coefficients", {}).items():
        value = features.get(feature)
        if value is None:
            profile = robust_profile("unit_242000_telemetry", feature)
            value = profile.get("median")
        if value is None:
            return None
        prediction += float(coef) * float(value)
    return round(prediction, 6)
