"""Read-only point-2 quality diagnostics. Never an input to the decision gate."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import median

from .analytics import clean, connection, parse_time, settings
from .config import FRESHNESS, LIMS_PUBLICATION_DELAY_MINUTES

METRICS = {
    "t95": "lims.ht.2.95%.T",
    "cetane": "lims.ht.2.CetaneNumber",
    "density": "lims.ht.2.D15",
    "t50": "lims.ht.2.50%.T",
}
UNITS = {"t95": "°C", "cetane": "ед.цет.ч.", "density": "кг/м³", "t50": "°C"}
UNUSABLE = {"invalid", "conflict", "suspect", "flatline", "gap"}
ESTIMATE_MAX_AGE_MINUTES = 48 * 60


def quality_diagnostics(directory: Path, at: str) -> dict:
    origin = parse_time(at)
    cutoff = origin - timedelta(minutes=LIMS_PUBLICATION_DELAY_MINUTES)
    freshness_minutes = settings(directory).get("freshness_minutes", FRESHNESS).get("lims", FRESHNESS["lims"])
    with connection(directory, derived=False) as db:
        # Keep the latest samples even if flagged. A bad new sample must not be
        # hidden by a fallback to an older good one. Duplicates are not new labs.
        rows = db.execute("""
            SELECT metric_id, timestamp, value, flags, unit FROM raw
            WHERE metric_id IN (SELECT unnest(?)) AND timestamp <= ?
            QUALIFY dense_rank() OVER (PARTITION BY metric_id ORDER BY timestamp DESC) <= 3
            ORDER BY metric_id, timestamp DESC
        """, [list(METRICS.values()), cutoff]).fetchall()
    samples = defaultdict(dict)
    for metric, timestamp, value, flags, unit in rows:
        samples[metric].setdefault(timestamp, []).append((value, flags, unit))

    def observation(name, timestamp=None, copies=()):
        age = (origin - timestamp).total_seconds() / 60 if timestamp else None
        flags = set()
        for _, raw_flags, _ in copies:
            flags.update(filter(None, (raw_flags or "").split("|")))
        values = {v for v, _, _ in copies if v is not None and math.isfinite(v)}
        if len(values) > 1:
            flags.add("conflict")
        if copies and any(v is None or not math.isfinite(v) for v, _, _ in copies):
            flags.add("invalid")
        value = next(iter(values)) if len(values) == 1 else None
        units_ok = bool(copies) and all(u == UNITS[name] for _, _, u in copies)
        reasons = []
        if not timestamp:
            reasons.append("Нет опубликованного анализа на выбранный момент.")
        else:
            if value is None or flags & UNUSABLE:
                reasons.append("Анализ недостоверен или помечен для проверки" + (": " + ", ".join(sorted(flags)) if flags else "") + ".")
            if not units_ok:
                reasons.append("Единицы измерения не подтверждены для расчёта.")
            if age > freshness_minutes:
                reasons.append(f"Анализ устарел: возраст {age / 60:.1f} ч при пороге {freshness_minutes / 60:g} ч.")
        return {
            "metric_id": METRICS[name], "value": value,
            "unit": " / ".join(sorted({u or "не указаны" for _, _, u in copies})) if copies else UNITS[name],
            "expected_unit": UNITS[name],
            "source_units": sorted({u or "не указаны" for _, _, u in copies}),
            "timestamp": timestamp,
            "available_at": timestamp + timedelta(minutes=LIMS_PUBLICATION_DELAY_MINUTES) if timestamp else None,
            "age_hours": age / 60 if age is not None else None,
            "freshness": "missing" if timestamp is None else "fresh" if age <= freshness_minutes else "stale",
            "flags": sorted(flags), "usable": not reasons, "reasons": reasons,
            "valid_value": value is not None and not flags & UNUSABLE and units_ok,
        }

    history = {name: [observation(name, ts, copies) for ts, copies in samples[mid].items()]
               for name, mid in METRICS.items()}
    latest = {name: records[0] if records else observation(name) for name, records in history.items()}

    def fresh_for_estimate(item):
        return item["valid_value"] and item["age_hours"] <= ESTIMATE_MAX_AGE_MINUTES / 60

    t95_inputs = history["t95"]
    t95_reasons = []
    if not fresh_for_estimate(latest["t95"]):
        t95_reasons.append("Для оценки нужен достоверный опубликованный T95 не старше 48 ч.")
    if any(not row["valid_value"] for row in t95_inputs):
        t95_reasons.append("Среди последних анализов есть недостоверные значения; они не заменяются более старыми.")
    t95_value = median(row["value"] for row in t95_inputs) if t95_inputs and not t95_reasons else None

    index_inputs = [latest["density"], latest["t50"]]
    index_reasons = []
    for label, row in zip(("Плотность D15", "T50"), index_inputs):
        if not fresh_for_estimate(row):
            index_reasons.append(f"{label}: нужен достоверный опубликованный анализ не старше 48 ч.")
    index_value = None
    if not index_reasons:
        density, t50 = [row["value"] for row in index_inputs]
        if not (700 <= density <= 1000 and 100 <= t50 <= 450):
            index_reasons.append("D15 или T50 вне проверяемого диапазона формулы (700–1000 кг/м³; 100–450 °C).")
        else:
            d = density / 1000
            index_value = 454.74 - 1641.416*d + 774.74*d*d - .554*t50 + 97.803*math.log10(t50)**2

    return clean({
        "at": origin, "scope": "Гидроочистка, точка 2; исходные наблюдения до сценарных изменений и блендинга",
        "diagnostic_only": True, "can_authorize": False,
        "lims_publication_delay_hours": LIMS_PUBLICATION_DELAY_MINUTES / 60,
        "measurement_freshness_hours": freshness_minutes / 60,
        "estimate_max_age_hours": ESTIMATE_MAX_AGE_MINUTES / 60,
        "t95": {
            "measurement": latest["t95"],
            "reference_warning": (
                "В последней пробе T95 выше контрольного ориентира 360 °C. Медиана не отменяет это измерение."
                if latest["t95"]["valid_value"] and latest["t95"]["value"] > 360 else None
            ),
            "estimate": {
                "method": "median_last_3_published", "value": t95_value, "unit": "°C",
                "status": "available" if t95_value is not None else "unavailable",
                "inputs": t95_inputs, "reasons": t95_reasons,
                "limitations": [
                    "Медиана до трёх последних различных проб. Порог 48 ч относится к самой новой пробе; старые входы показаны отдельно.",
                    "Условная оценка при сохранении режима. Проверялась на горизонтах 0 и 180 мин; реакция на управление не установлена.",
                    "В аудите 2026 медиана пропустила все 8 превышений 360 °C на общей выборке. Надёжная граница для подтверждения качества отсутствует.",
                ],
            },
        },
        "cetane": {
            "measurement": latest["cetane"],
            "reference_warning": (
                "В последней пробе цетановое число ниже контрольного ориентира 51. Расчётный индекс не отменяет это измерение."
                if latest["cetane"]["valid_value"] and latest["cetane"]["value"] < 51 else None
            ),
            "estimate": {
                "method": "calculated_cetane_index_d976", "value": index_value, "unit": "индекс",
                "status": "available" if index_value is not None else "unavailable",
                "inputs": index_inputs, "reasons": index_reasons,
                "limitations": [
                    "Расчётный цетановый индекс по D15 и T50 не является измеренным цетановым числом и не подтверждает эффект присадки.",
                    "Используются последние доступные лабораторные входы; при разных временах отбора это приближённое сочетание проб.",
                    "В исследовании хакатонного набора было 42 анализа цетана и одно значение ниже 51. По доступным на тот момент данным индекс пропустил это нарушение.",
                ],
            },
        },
        "evidence_report": "reports/modeling/cetane-t95/README.md",
    })
