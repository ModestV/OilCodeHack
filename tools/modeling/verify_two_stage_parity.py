"""Runtime/offline parity of the two-stage v4r forecast on every walk-forward origin and horizon.

Offline (``two_stage_forecast.py``) builds features from the full dataset;
runtime (``backend/forecast_two_stage.py``) rebuilds them from a bounded
history window at one origin and picks the walk-forward model set by the
origin.  Status, abstain reasons, model set, path, point, 80% interval and
P(>10) must agree, otherwise the offline metrics do not describe the served
model.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOLERANCE = 1e-9
_DATASET: Path | None = None


def _init(dataset: str) -> None:
    global _DATASET
    import warnings
    warnings.filterwarnings("ignore")
    _DATASET = Path(dataset)


def _check(row: dict) -> dict:
    from backend.forecast_two_stage import forecast_sulfur
    horizon = int(row["horizon_minutes"])
    runtime = forecast_sulfur(_DATASET, str(row["prediction_origin"]), horizon_minutes=horizon)
    offline_reasons = row["abstain_reasons"] if isinstance(row["abstain_reasons"], str) else ""
    item = {"origin": str(row["prediction_origin"]), "horizon_minutes": horizon, "offline_fit_end": str(row["model_fit_end"])[:10],
            "runtime_fit_end": (runtime["model"]["fit_end"] or "")[:10], "offline_status": row["forecast_status"],
            "runtime_status": runtime["status"], "offline_reasons": offline_reasons, "runtime_reasons": "|".join(runtime["reasons"])}
    problems = []
    if item["runtime_status"] != item["offline_status"]:
        problems.append("status")
    if item["offline_reasons"] != item["runtime_reasons"]:
        problems.append("reasons")
    if item["runtime_fit_end"] != item["offline_fit_end"]:
        problems.append("model_fit_end")
    if runtime["status"] == "ok":
        for key in ("prediction", "prediction_lower", "prediction_upper", "exceedance_probability"):
            offline, served = float(row[key]), runtime[key]
            item[f"max_abs_diff_{key}"] = abs(served - offline)
            if not np.isclose(served, offline, rtol=TOLERANCE, atol=TOLERANCE):
                problems.append(key)
        if horizon:
            if runtime["stage1"]["status"] != row["stage1_status"]:
                problems.append("stage1_status")
        elif not np.isclose(runtime["nowcast"]["prediction"], float(row["prediction"]), rtol=TOLERANCE, atol=TOLERANCE):
            problems.append("nowcast")
        # One terminal level: trajectory end, point, interval and probability at the requested horizon.
        terminal = [r for r in runtime["horizons"] if r["minutes"] == horizon][0]
        if not (np.isclose(terminal["prediction"], runtime["prediction"], rtol=0, atol=1e-12)
                and np.isclose(terminal["exceedance_probability"], runtime["exceedance_probability"], rtol=0, atol=1e-12)
                and runtime["prediction_lower"] <= runtime["prediction"] <= runtime["prediction_upper"]):
            problems.append("terminal_level")
    item["problems"] = problems
    return item


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, default=ROOT / "reports/modeling/two-stage-v4r/predictions.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    from backend.forecast_two_stage import ARTIFACT, load_artifact
    artifact = load_artifact(ARTIFACT)
    predictions = pd.read_csv(args.predictions)
    predictions = predictions[predictions.split.eq("walk_forward")]
    rows = predictions.to_dict("records")
    with ProcessPoolExecutor(args.workers, initializer=_init, initargs=(str(args.dataset.resolve()),)) as pool:
        evidence = list(pool.map(_check, rows, chunksize=16))
    failures = [e for e in evidence if e["problems"]]
    frame = pd.DataFrame(evidence)
    frame["year"] = frame.origin.str[:4]
    by_group = (frame.assign(ok=frame.problems.str.len().eq(0))
                .groupby(["year", "horizon_minutes", "runtime_status"]).ok.agg(["count", "sum"]).reset_index()
                .rename(columns={"count": "checked", "sum": "matched"}).to_dict("records"))
    diffs = {k: float(frame[k].max()) for k in frame if k.startswith("max_abs_diff_")}
    report = {"passed": not failures, "artifact_sha256": artifact["sha256"], "tolerance": TOLERANCE,
              "protocol": "every walk-forward origin x horizon (2024-2026), no sampling",
              "checked": len(evidence), "failures": failures[:200], "failure_count": len(failures),
              "by_year_horizon_status": by_group, "max_abs_diff": diffs}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("passed", "checked", "failure_count", "max_abs_diff")}, indent=2))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
