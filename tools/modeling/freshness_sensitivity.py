"""Separate the forecast-model effect from the CN/T95 freshness effect on decision coverage.

The v4 candidate raised recommendation coverage mostly by accepting cetane up
to 60 days and T95 up to 7 days old instead of the 48-hour LIMS policy.  This
replay keeps the runtime policy (48 h) and reports:

1. ``policy_48h`` — real ``make_decision`` coverage with the v3 forecast and
   with the opt-in v4r adapter (same origins, same dataset);
2. ``research_60d_7d`` — *research only, never applied*: an upper bound of the
   decisions that would be unblocked if cetane <= 60 d and T95 <= 7 d were
   accepted (only decisions whose sole blocking reasons are CN/T95 staleness);
3. a sensitivity table by cetane age, T95 age and regime class.

Coverage is not correctness: this measures how often the contour may act,
not whether the action is right.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HORIZON = 180
RELAXED = {"cetane": pd.Timedelta(days=60), "t95": pd.Timedelta(days=7)}
STALE = re.compile(r"^(cetane|t95): измерение не свежее")
AGE_BINS = [0, 2, 7, 30, 60, np.inf]
AGE_LABELS = ["<=2d", "2-7d", "7-30d", "30-60d", ">60d"]
_STATE: dict = {}


def _init(dataset: str, model: str) -> None:
    import warnings
    warnings.filterwarnings("ignore")
    if model == "v4r":
        os.environ["OILCODE_FORECAST_MODEL"] = "two-stage-v4r"
    else:
        os.environ.pop("OILCODE_FORECAST_MODEL", None)
    _STATE["dataset"] = Path(dataset)


def _decide(at: str) -> dict:
    from backend.agents import make_decision
    from backend.scenarios import ScenarioRequest
    result = make_decision(_STATE["dataset"], ScenarioRequest(at=at, horizon_minutes=HORIZON))
    other = result["agents"]["quality"]["evidence"]["other_quality"]
    reasons = list(dict.fromkeys(result["safety_gate"]["reasons"]))
    ages = {}
    for name in ("cetane", "t95"):
        ts = other[name].get("timestamp")
        ages[name] = (pd.Timestamp(at) - pd.Timestamp(ts)).total_seconds() / 86400 if ts else None
    severity = result["agents"]["reliability"].get("regime_severity") or {}
    forecast = result.get("forecast") or {}
    return {"at": at, "status": result["status"], "reasons": reasons,
            "forecast_status": forecast.get("status"), "forecast_model": (forecast.get("model") or {}).get("family", "v3"),
            "cetane_age_days": ages["cetane"], "t95_age_days": ages["t95"],
            "cetane_fresh": other["cetane"].get("freshness") == "fresh", "t95_fresh": other["t95"].get("freshness") == "fresh",
            "regime_class": severity.get("class")}


def replay(dataset: Path, origins: list[str], model: str, workers: int) -> pd.DataFrame:
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(str(dataset), model)) as pool:
        rows = list(pool.map(_decide, origins, chunksize=8))
    frame = pd.DataFrame(rows)
    frame["model"] = model
    return frame


def annotate(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["recommended"] = frame.status.eq("recommendation")
    only_stale = frame.reasons.map(lambda r: bool(r) and all(STALE.match(x) for x in r))
    stale_cn = frame.reasons.map(lambda r: any(x.startswith("cetane: измерение не свежее") for x in r))
    stale_t95 = frame.reasons.map(lambda r: any(x.startswith("t95: измерение не свежее") for x in r))
    within = ((~stale_cn | (frame.cetane_age_days <= RELAXED["cetane"].days))
              & (~stale_t95 | (frame.t95_age_days <= RELAXED["t95"].days)))
    frame["blocked_only_by_cn_t95_staleness"] = ~frame.recommended & only_stale
    frame["research_relaxed_upper_bound"] = frame.recommended | (frame.blocked_only_by_cn_t95_staleness & within)
    frame["year"] = frame["at"].str[:4]
    frame["cetane_age_bin"] = pd.cut(frame.cetane_age_days, AGE_BINS, labels=AGE_LABELS, right=True).astype(str)
    frame["t95_age_bin"] = pd.cut(frame.t95_age_days, AGE_BINS, labels=AGE_LABELS, right=True).astype(str)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    labs = pd.read_csv(ROOT / "reports/modeling/two-stage-v4r/predictions.csv", usecols=["split", "horizon_minutes", "prediction_origin"])
    origins = sorted(labs[labs.split.eq("walk_forward") & labs.horizon_minutes.eq(HORIZON)].prediction_origin.unique())
    frames = [annotate(replay(args.dataset.resolve(), origins, model, args.workers)) for model in ("v3", "v4r")]
    rows = pd.concat(frames, ignore_index=True)
    args.output.mkdir(parents=True, exist_ok=True)
    rows.assign(reasons=rows.reasons.map("|".join)).to_csv(args.output / "freshness_rows.csv", index=False)

    def rate(group):
        return pd.Series({"origins": len(group), "recommended_48h": int(group.recommended.sum()),
                          "coverage_48h": float(group.recommended.mean()),
                          "blocked_only_by_cn_t95_staleness": int(group.blocked_only_by_cn_t95_staleness.sum()),
                          "research_relaxed_upper_bound": int(group.research_relaxed_upper_bound.sum()),
                          "coverage_research_upper_bound": float(group.research_relaxed_upper_bound.mean()),
                          "forecast_ok": int(group.forecast_status.eq("ok").sum())})

    by_year = rows.groupby(["model", "year"]).apply(rate, include_groups=False).reset_index()
    overall = rows.groupby("model").apply(rate, include_groups=False).reset_index()
    sensitivity = (rows.groupby(["model", "cetane_age_bin", "t95_age_bin", "regime_class"], dropna=False)
                   .apply(rate, include_groups=False).reset_index())
    sensitivity.to_csv(args.output / "freshness_sensitivity.csv", index=False)
    paired = frames[0][["at", "recommended"]].merge(frames[1][["at", "recommended"]], on="at", suffixes=("_v3", "_v4r"))
    report = {
        "protocol": "real make_decision at LIMS origins 2024-2026 minus 180 min; horizon 180; runtime freshness 48 h unchanged",
        "dataset_sha256": hashlib.sha256((args.dataset / "observations.parquet").read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "policy_48h": {"overall": overall.to_dict("records"), "by_year": by_year.to_dict("records"),
                       "paired_v3_vs_v4r": {"both": int((paired.recommended_v3 & paired.recommended_v4r).sum()),
                                            "only_v3": int((paired.recommended_v3 & ~paired.recommended_v4r).sum()),
                                            "only_v4r": int((~paired.recommended_v3 & paired.recommended_v4r).sum()),
                                            "neither": int((~paired.recommended_v3 & ~paired.recommended_v4r).sum())}},
        "research_60d_7d": {"research_only": True, "applied_in_runtime": False,
                            "definition": "upper bound: decisions whose only blocking reasons are CN/T95 staleness and whose "
                                          "CN age <= 60 d and T95 age <= 7 d; downstream checks after the gate are not re-run",
                            "note": "no technological or organisational justification for relaxing 48 h was found; not a model gain"},
        "reason_counts": {m: pd.Series([x for r in g.reasons for x in set(r)]).value_counts().head(15).to_dict()
                          for m, g in rows.groupby("model")},
        "age_quantiles_days": {name: rows[rows.model.eq("v3")][f"{name}_age_days"].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(2).to_dict()
                               for name in ("cetane", "t95")},
    }
    (args.output / "freshness_sensitivity.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("policy_48h",)}, ensure_ascii=False, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
