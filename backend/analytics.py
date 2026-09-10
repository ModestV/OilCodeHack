"""Read-only analytics over immutable observations; no browser-side scientific maths."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import duckdb

from .config import FRESHNESS

VALID = "value IS NOT NULL AND isfinite(value) AND NOT contains(flags, 'invalid') AND NOT contains(flags, 'conflict')"
SUSPECT = "(contains(flags, 'flatline') OR contains(flags, 'suspect'))"


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Неверная дата: требуется ISO YYYY-MM-DDTHH:MM:SS") from exc
    if parsed.tzinfo is not None:
        raise ValueError("Передайте время источника без часового пояса")
    return parsed


def interval(start: str, end: str) -> tuple[datetime, datetime]:
    a, b = parse_time(start), parse_time(end)
    if b <= a:
        raise ValueError("Конец периода должен быть позже начала")
    return a, b


def clean(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def records(cursor):
    keys = [c[0] for c in cursor.description]
    return [clean(dict(zip(keys, row))) for row in cursor.fetchall()]


def manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text())


def metric_catalog(directory: Path) -> list[dict]:
    metrics = [dict(m) for m in manifest(directory).get("metrics", [])]
    by_id = {m["id"]: m for m in metrics}
    for source in ("lims", "pak"):
        base = "lims.ht.2.Mg.Sulfur" if source == "lims" else "pak.ht.Mg.Sulfur"
        if base not in by_id:
            continue
        for name, title in (("margin", "Запас по сере"), ("excess", "Превышение серы")):
            metrics.append(
                {
                    **by_id[base],
                    "id": f"derived.sulfur_{name}.{source}",
                    "label": f"{title} · {source.upper()}",
                    "primary": False,
                    "group": "Дополнительные расчёты",
                    "description": "Контрольный ориентир ТЗ: 10 мг/кг",
                }
            )
    for key, first, second, title in (
        ("k2", "T33", "T20", "Перепад температур К-2"),
        ("k10", "T48", "T49", "Перепад температур К-10"),
    ):
        if all(f"avt.{t}" in by_id for t in (first, second)):
            metrics.append(
                {
                    "id": f"derived.avt.delta_{key}",
                    "label": title,
                    "source": "kip",
                    "plant": "avt",
                    "code": f"{first} − {second}",
                    "unit": "°C",
                    "unit_status": "inferred",
                    "group": "Дополнительные расчёты",
                    "description": "Разность температур; не оценка КПД или безопасности",
                }
            )
    for key, title, expression in (
        ("diesel_flow", "Сумма дизельных отборов", "F30 + F32"),
        ("heavy_fraction", "Доля тяжёлого отбора", "F30 / (F30 + F32)"),
        ("diesel_volume", "Объём дизельных отборов за период", "Интеграл расхода по времени"),
    ):
        if all(mid in by_id for mid in ("avt.F30", "avt.F32")):
            metrics.append(
                {
                    "id": f"disabled.avt.{key}",
                    "label": title,
                    "source": "kip",
                    "plant": "avt",
                    "code": expression,
                    "unit": None,
                    "unit_status": "unknown",
                    "group": "Расчёты, требующие уточнения",
                    "available": False,
                    "mapping_warning": "Нужны подтверждённые единицы и сопоставимость расходов F30/F32",
                }
            )
    return metrics


@contextmanager
def connection(directory: Path, derived: bool = True):
    db = duckdb.connect(":memory:")
    try:
        db.execute("SET threads=2")
        db.execute("SET memory_limit='768MB'")
        path = str(directory / "observations.parquet").replace("'", "''")
        db.execute(f"CREATE VIEW raw AS SELECT * FROM read_parquet('{path}')")
        branches = ["SELECT * FROM raw"]
        if derived:
            for source in ("lims", "pak"):
                base = "lims.ht.2.Mg.Sulfur" if source == "lims" else "pak.ht.Mg.Sulfur"
                for key, expression in (
                    ("margin", "10-value"),
                    ("excess", "greatest(value-10, 0)"),
                ):
                    branches.append(f"""SELECT 'derived.sulfur_{key}.{source}' AS metric_id, timestamp,
                        CASE WHEN {VALID} THEN {expression} ELSE NULL END AS value,
                        source, unit, flags, source_file, source_row FROM raw WHERE metric_id='{base}'""")
            for key, a, b in (("k2", "T33", "T20"), ("k10", "T48", "T49")):
                branches.append(f"""SELECT 'derived.avt.delta_{key}' AS metric_id, a.timestamp,
                    a.value-b.value AS value, 'kip' AS source, '°C' AS unit,
                    concat_ws('|', nullif(a.flags,''), nullif(b.flags,'')) AS flags,
                    a.source_file, a.source_row
                    FROM (SELECT * FROM raw WHERE metric_id='avt.{a}') a
                    JOIN (SELECT * FROM raw WHERE metric_id='avt.{b}') b ON a.timestamp=b.timestamp""")
        db.execute("CREATE VIEW obs AS " + " UNION ALL ".join(branches))
        yield db
    finally:
        db.close()


def settings(directory: Path) -> dict:
    path = directory / "settings.json"
    return json.loads(path.read_text()) if path.exists() else {"freshness_minutes": FRESHNESS}


def snapshot(directory: Path, at: str) -> dict:
    target = parse_time(at)
    with connection(directory) as db:
        rows = records(
            db.execute(
                """SELECT metric_id,
            arg_max(struct_pack("timestamp":=timestamp,"value":=value,flags:=flags,source_row:=source_row),
                struct_pack("timestamp":=timestamp,source_row:=source_row),2) AS recent
            FROM obs WHERE timestamp<=? GROUP BY metric_id""",
                [target],
            )
        )
    grouped: dict[str, list] = {}
    for row in rows:
        grouped[row["metric_id"]] = row["recent"]
    values, alerts = [], []
    freshness = settings(directory)["freshness_minutes"]
    for metric in metric_catalog(directory):
        data = grouped.get(metric["id"], [])
        current = data[0] if data else None
        flags = list(filter(None, current["flags"].split("|"))) if current else []
        age = (target - parse_time(current["timestamp"])).total_seconds() / 60 if current else None
        state = (
            "missing"
            if not current
            else "fresh"
            if age <= freshness.get(metric["source"], 10)
            else "stale"
        )
        value = current["value"] if current and not ({"invalid", "conflict"} & set(flags)) else None
        prior = data[1] if len(data) > 1 else None
        delta = (
            value - prior["value"]
            if value is not None
            and prior
            and prior["value"] is not None
            and not any(f in prior["flags"] for f in ("invalid", "conflict"))
            else None
        )
        reason = "Нет измерений не позже выбранного момента" if not current else None
        if value is None and current:
            reason = "Недостоверное или конфликтующее измерение"
        values.append(
            {
                "metric_id": metric["id"],
                "value": value,
                "timestamp": current["timestamp"] if current else None,
                "age_minutes": age,
                "unit": metric.get("unit"),
                "flags": flags,
                "freshness": state,
                "delta": delta,
                "reason": reason,
            }
        )
        if metric["id"] in ("lims.ht.2.Mg.Sulfur", "pak.ht.Mg.Sulfur"):
            if value is not None and value > 10:
                alerts.append(
                    {
                        "severity": "danger",
                        "metric_id": metric["id"],
                        "message": f"{metric['source'].upper()}: сера {value:.2f} мг/кг выше 10 мг/кг в последнем измерении",
                        "timestamp": current["timestamp"],
                    }
                )
            if state != "fresh":
                alerts.append(
                    {
                        "severity": "warning",
                        "metric_id": metric["id"],
                        "message": f"{metric['source'].upper()}: "
                        + ("анализ серы устарел" if current else "нет анализа серы"),
                    }
                )
            if flags:
                alerts.append(
                    {
                        "severity": "warning",
                        "metric_id": metric["id"],
                        "message": f"{metric['source'].upper()}: отметки качества данных — {', '.join(flags)}",
                    }
                )
    return clean({"at": target, "values": values, "alerts": alerts})


def valid_sql(exclude: bool = False) -> str:
    return VALID + (f" AND NOT {SUSPECT}" if exclude else "")


@lru_cache(maxsize=20)
def summary(directory: Path, start: str, end: str, exclude: bool = False) -> dict:
    a, b = interval(start, end)
    previous = a - (b - a)
    valid = valid_sql(exclude)
    with connection(directory) as db:
        rows = records(
            db.execute(
                f"""WITH base AS (
            SELECT *, CASE WHEN {valid} THEN value ELSE NULL END AS v,
            timestamp >= ? AS current FROM obs WHERE timestamp >= ? AND timestamp < ?),
            stats_raw AS (SELECT metric_id, current, count(v) AS count,
            count(*) FILTER(WHERE NOT ({VALID})) AS invalid_count,
            count(*) FILTER(WHERE {SUSPECT}) AS suspect_count,
            avg(v) AS mean, min(v) AS min, max(v) AS max,
            arg_min(timestamp, v) AS min_at, arg_max(timestamp,v) AS max_at,
            quantile_cont(v,[0.05,0.25,0.5,0.75,0.95]) AS quantiles,
            stddev_pop(v) AS std,
            max(v)-min(v) AS range,
            first(v ORDER BY timestamp) FILTER(WHERE v IS NOT NULL) AS first,
            last(v ORDER BY timestamp) FILTER(WHERE v IS NOT NULL) AS last,
            min(timestamp) FILTER(WHERE v IS NOT NULL) AS first_at,
            max(timestamp) FILTER(WHERE v IS NOT NULL) AS last_at
            FROM base GROUP BY metric_id,current),
            stats AS (SELECT * EXCLUDE(quantiles), quantiles[3] AS median,
            quantiles[1] AS p05, quantiles[5] AS p95,
            quantiles[4]-quantiles[2] AS iqr FROM stats_raw)
            SELECT c.* EXCLUDE(current), c.last-c.first AS change,
            p.median AS previous_median, c.median-p.median AS median_change
            FROM stats c LEFT JOIN stats p ON c.metric_id=p.metric_id AND NOT p.current
            WHERE c.current""",
                [a, previous, b],
            )
        )
        sulfur = records(
            db.execute(
                f"""SELECT count(*) AS lab_count,
            count(*) FILTER(WHERE value>10) AS lab_exceed_count,
            avg(CASE WHEN value>10 THEN 1.0 ELSE 0.0 END) AS lab_exceed_fraction
            FROM raw WHERE metric_id='lims.ht.2.Mg.Sulfur' AND timestamp>=? AND timestamp<? AND {valid}""",
                [a, b],
            )
        )[0]
        duration = records(
            db.execute(
                f"""WITH seq AS (
            SELECT *, lead(timestamp) OVER(ORDER BY timestamp) AS next_time FROM raw
            WHERE metric_id='pak.ht.Mg.Sulfur' AND timestamp>=? AND timestamp<?),
            durations AS (SELECT *, greatest(0, epoch(least(coalesce(next_time,timestamp+INTERVAL 10 MINUTE),
            timestamp+INTERVAL 10 MINUTE, CAST(? AS TIMESTAMP)) - greatest(timestamp,CAST(? AS TIMESTAMP)))/60) AS minutes FROM seq)
            SELECT coalesce(sum(minutes) FILTER(WHERE {VALID} AND NOT {SUSPECT}),0) AS pak_observed_minutes,
            coalesce(sum(minutes) FILTER(WHERE {VALID} AND NOT {SUSPECT} AND value>10),0) AS pak_exceed_minutes,
            coalesce(sum(minutes) FILTER(WHERE {SUSPECT}),0) AS pak_suspect_minutes
            FROM durations""",
                [a - timedelta(minutes=10), b, b, a],
            )
        )[0]
        sulfur.update(duration)
        sulfur["pak_coverage_fraction"] = duration["pak_observed_minutes"] / (
            (b - a).total_seconds() / 60
        )
        agreement = []
        for code in ("Mg.Sulfur", "D15"):
            pair = records(
                db.execute(
                    f"""WITH l AS (SELECT timestamp,value FROM raw
                WHERE metric_id=? AND timestamp>=? AND timestamp<? AND {VALID} AND NOT {SUSPECT}),
                p AS (SELECT timestamp,value FROM raw WHERE metric_id=? AND timestamp>=? AND timestamp<? AND {VALID} AND NOT {SUSPECT})
                SELECT count(*) AS n,avg(p.value-l.value) AS bias,avg(abs(p.value-l.value)) AS mae
                FROM l ASOF LEFT JOIN p ON l.timestamp>=p.timestamp
                WHERE l.timestamp-p.timestamp<=INTERVAL 10 MINUTE""",
                    [
                        f"lims.ht.2.{code}",
                        a,
                        b,
                        f"pak.ht.{code}",
                        a - timedelta(minutes=10),
                        b,
                    ],
                )
            )[0]
            agreement.append({"metric_id": f"pak.ht.{code}", **pair})
    # Return empty stats for missing signals rather than substituting carried-forward labs.
    indexed = {r["metric_id"]: r for r in rows}
    fields = [
        "mean",
        "median",
        "min",
        "max",
        "min_at",
        "max_at",
        "p05",
        "p95",
        "std",
        "iqr",
        "range",
        "first",
        "last",
        "first_at",
        "last_at",
        "change",
        "previous_median",
        "median_change",
    ]
    for m in metric_catalog(directory):
        if m["id"] not in indexed:
            indexed[m["id"]] = {
                "metric_id": m["id"],
                "count": 0,
                "invalid_count": 0,
                "suspect_count": 0,
                **dict.fromkeys(fields),
            }
    return clean(
        {
            "from": a,
            "to": b,
            "comparison_from": previous,
            "comparison_to": a,
            "metrics": list(indexed.values()),
            "sulfur": sulfur,
            "agreement": agreement,
        }
    )


def series(
    directory: Path,
    ids: list[str],
    start: str,
    end: str,
    limit: int = 600,
    exclude: bool = False,
) -> dict:
    a, b = interval(start, end)
    width = max(1, (b - a).total_seconds() / limit)
    valid = valid_sql(exclude)
    result = []
    with connection(directory) as db:
        counts = dict(
            db.execute(
                "SELECT metric_id,count(*) FROM obs WHERE metric_id IN (SELECT unnest(?)) AND timestamp>=? AND timestamp<? GROUP BY metric_id",
                [ids, a, b],
            ).fetchall()
        )
        for mid in ids:
            if counts.get(mid, 0) <= limit:
                rows = records(
                    db.execute(
                        f"""SELECT timestamp, CASE WHEN {valid} THEN value ELSE NULL END AS value,
                    CASE WHEN {valid} THEN value ELSE NULL END AS min, CASE WHEN {valid} THEN value ELSE NULL END AS max,
                    flags, 1 AS count FROM obs WHERE metric_id=? AND timestamp>=? AND timestamp<? ORDER BY timestamp""",
                        [mid, a, b],
                    )
                )
            else:
                rows = records(
                    db.execute(
                        f"""WITH buckets AS (SELECT *, floor(epoch(timestamp-CAST(? AS TIMESTAMP)) / ?) AS bucket,
                    CASE WHEN {valid} THEN value ELSE NULL END AS v FROM obs WHERE metric_id=? AND timestamp>=? AND timestamp<?)
                    SELECT min(timestamp) AS timestamp, median(v) AS value, min(v) AS min, max(v) AS max,
                    string_agg(DISTINCT flags,'|') AS flags, count(v) AS count FROM buckets GROUP BY bucket ORDER BY timestamp""",
                        [a, width, mid, a, b],
                    )
                )
            points = []
            for row in rows:
                row["flags"] = sorted(set(filter(None, row["flags"].split("|"))))
                if (
                    points
                    and not mid.startswith("lims.")
                    and (
                        parse_time(row["timestamp"]) - parse_time(points[-1]["timestamp"])
                    ).total_seconds()
                    > max(1200, width * 2)
                ):
                    points.append(
                        {
                            "timestamp": (
                                parse_time(points[-1]["timestamp"])
                                + timedelta(seconds=max(600, width))
                            ).isoformat(),
                            "value": None,
                            "min": None,
                            "max": None,
                            "flags": ["gap"],
                            "count": 0,
                        }
                    )
                points.append(row)
            result.append(
                {
                    "metric_id": mid,
                    "points": points,
                    "aggregated": counts.get(mid, 0) > limit,
                }
            )
    return {"series": result}


def distribution(directory: Path, mid: str, start: str, end: str, exclude: bool = False) -> dict:
    a, b = interval(start, end)
    valid = valid_sql(exclude)
    with connection(directory) as db:
        basic = records(
            db.execute(
                f"SELECT count(*) AS count,min(value) AS min,max(value) AS max,quantile_cont(value,[0,0.25,0.5,0.75,1]) AS quartiles FROM obs WHERE metric_id=? AND timestamp>=? AND timestamp<? AND {valid}",
                [mid, a, b],
            )
        )[0]
        if not basic["count"]:
            return {"metric_id": mid, "count": 0, "bins": [], "quartiles": []}
        width = (basic["max"] - basic["min"]) / 20 if basic["max"] > basic["min"] else 1
        rows = records(
            db.execute(
                f"""SELECT least(19,cast(floor((value-?)/?) AS INTEGER)) AS bin, count(*) AS count FROM obs
            WHERE metric_id=? AND timestamp>=? AND timestamp<? AND {valid} GROUP BY bin ORDER BY bin""",
                [basic["min"], width, mid, a, b],
            )
        )
        counts = {r["bin"]: r["count"] for r in rows}
        bins = [
            {
                "from": basic["min"] + i * width,
                "to": basic["min"] + (i + 1) * width,
                "count": counts.get(i, 0),
            }
            for i in range(20 if basic["min"] != basic["max"] else 1)
        ]
    return {
        "metric_id": mid,
        "count": basic["count"],
        "bins": bins,
        "quartiles": basic["quartiles"],
    }


@lru_cache(maxsize=4)
def quality(directory: Path) -> dict:
    m = manifest(directory)
    with connection(directory, derived=False) as db:
        rows = records(
            db.execute(f"""SELECT metric_id,count(*) AS count,
            count(*) FILTER(WHERE NOT({VALID})) AS invalid_count,
            count(*) FILTER(WHERE {SUSPECT}) AS suspect_count,
            count(*) FILTER(WHERE contains(flags,'flatline')) AS flatline_count,
            min(timestamp) AS start,max(timestamp) AS end FROM raw GROUP BY metric_id""")
        )
    return {
        "sources": m.get("sources", []),
        "issues": m.get("issues", []),
        "assumptions": m.get("assumptions", []),
        "metrics": rows,
    }


def distillation(directory: Path, at: str) -> dict:
    target = parse_time(at)
    ids = ["lims.ht.2.IBP.T", "lims.ht.2.50%.T", "lims.ht.2.90%.T", "lims.ht.2.95%.T"]
    with connection(directory, derived=False) as db:
        timestamp = db.execute(
            f"""SELECT timestamp FROM raw WHERE metric_id IN (SELECT unnest(?))
            AND timestamp<=? AND {VALID} AND NOT {SUSPECT}
            GROUP BY timestamp HAVING count(DISTINCT metric_id)>=2 ORDER BY timestamp DESC LIMIT 1""",
            [ids, target],
        ).fetchone()
        if not timestamp:
            return {
                "timestamp": None,
                "points": [],
                "reason": "Нет двух фракционных показателей с одинаковым временем пробы",
            }
        rows = records(
            db.execute(
                f"SELECT metric_id,value FROM raw WHERE metric_id IN (SELECT unnest(?)) AND timestamp=? AND {VALID} AND NOT {SUSPECT}",
                [ids, timestamp[0]],
            )
        )
    percent = {"IBP.T": 0, "50%.T": 50, "90%.T": 90, "95%.T": 95}
    points = sorted(
        [
            {
                "fraction": percent[r["metric_id"].removeprefix("lims.ht.2.")],
                "temperature": r["value"],
                "metric_id": r["metric_id"],
            }
            for r in rows
        ],
        key=lambda r: r["fraction"],
    )
    monotonic = all(
        right["temperature"] >= left["temperature"] for left, right in zip(points, points[1:])
    )
    return clean(
        {
            "timestamp": timestamp[0],
            "points": points,
            "age_minutes": (target - timestamp[0]).total_seconds() / 60,
            "reason": None
            if monotonic
            else "Нарушен порядок температур фракционного состава; проверьте пробу",
        }
    )
