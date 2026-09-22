"""Lower confidence bound for a rarely sampled lab property (cetane number).

The laboratory measures the product cetane number about once a month (median
gap 29 days in 2023–2025), so a 48-hour freshness rule makes the constraint
unverifiable by construction.  Instead the last published sample is kept and
widened by the drift observed between consecutive samples: a random walk with
σ per √day, estimated only from samples published before the decision moment.
The one-sided 90% bound ``value - 1.2816·σ·√age`` is what the constraint is
checked against; if it is below 51 the optimiser may dose cetane improver to
cover the gap.  Beyond ``MAX_AGE_DAYS`` the sample is not used at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from math import sqrt
from pathlib import Path

import duckdb
import numpy as np

CETANE_METRIC = "lims.ht.2.CetaneNumber"
# Hackathon LIMS 2023-01..2025-12, 35 consecutive pairs; used when the dataset
# has too little history before the decision moment.
DEFAULT_SIGMA_PER_SQRT_DAY = 0.311
MIN_PAIRS = 8
MIN_GAP_DAYS = 0.5
MAX_AGE_DAYS = 60.0
Z_ONE_SIDED_90 = 1.2816
LIMS_PUBLICATION_DELAY = timedelta(hours=4)


@lru_cache(maxsize=128)
def _sigma(path: str, mtime: float, metric_id: str, cutoff: str) -> tuple[float, int, str]:
    escaped = path.replace("'", "''")
    try:
        with duckdb.connect(":memory:") as db:
            rows = db.execute(
                f"""SELECT timestamp, value FROM read_parquet('{escaped}')
                    WHERE metric_id=? AND timestamp<=? AND value IS NOT NULL AND isfinite(value)
                      AND NOT contains(coalesce(flags, ''), 'invalid') AND NOT contains(coalesce(flags, ''), 'conflict')
                    ORDER BY timestamp""",
                [metric_id, datetime.fromisoformat(cutoff)],
            ).fetchall()
    except duckdb.Error:
        rows = []
    if len(rows) > 1:
        times = np.array([row[0] for row in rows], dtype="datetime64[s]")
        values = np.array([row[1] for row in rows], dtype=float)
        gaps = np.diff(times).astype("timedelta64[s]").astype(float) / 86400
        steps = np.diff(values)
        keep = gaps >= MIN_GAP_DAYS
        if keep.sum() >= MIN_PAIRS:
            return float(sqrt(np.mean(steps[keep] ** 2 / gaps[keep]))), int(keep.sum()), "dataset_before_decision"
    return DEFAULT_SIGMA_PER_SQRT_DAY, 0, "default_hackathon_lims_2023_2025"


def drift_sigma(directory: Path, at: datetime, metric_id: str = CETANE_METRIC) -> tuple[float, int, str]:
    """σ per √day from samples already published at ``at`` (day resolution, never later)."""
    path = directory / "observations.parquet"
    if not path.exists():
        return DEFAULT_SIGMA_PER_SQRT_DAY, 0, "default_hackathon_lims_2023_2025"
    day = datetime(at.year, at.month, at.day) - LIMS_PUBLICATION_DELAY
    return _sigma(str(path), path.stat().st_mtime, metric_id, day.isoformat())


def aged_lower_bound(value: float, age_days: float, sigma: float) -> dict:
    allowance = Z_ONE_SIDED_90 * sigma * sqrt(max(age_days, 0.0))
    return {"value": value, "age_days": age_days, "drift_sigma_per_sqrt_day": sigma,
            "allowance": allowance, "lower_bound": value - allowance,
            "max_age_days": MAX_AGE_DAYS, "usable": age_days <= MAX_AGE_DAYS,
            "basis": "one-sided 90% random-walk drift since the last lab sample"}
