"""Check that runtime forecasts reproduce the offline walk-forward predictions.

Offline (``sulfur_forecast.py``) builds features from the full imported
dataset; runtime (``backend/forecast.py``) rebuilds them from a bounded
history window at a single origin and picks the walk-forward model by the
origin.  Both must agree for the same origin and horizon — status and abstain
reasons, point prediction, 80% interval, P(>10) and the Stage-1 path —
otherwise the benchmark metrics do not describe the served model.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.forecast import ARTIFACT, _load_artifact, forecast_sulfur  # noqa: E402

TOLERANCE = 1e-6


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=6, help="Accepted rows per horizon and walk-forward model")
    parser.add_argument("--abstain-samples", type=int, default=2, help="Abstained rows per horizon and model")
    args = parser.parse_args()
    artifact = _load_artifact()
    predictions = pd.read_csv(ARTIFACT.parent / "predictions.csv")
    predictions = predictions[predictions.split == "walk_forward"]
    evidence, failures = [], []
    for fit_end in sorted(predictions.model_fit_end.unique()):
        for horizon in artifact["horizons_minutes"]:
            rows = predictions[(predictions.horizon_minutes == horizon) & (predictions.model_fit_end == fit_end)]
            picks = []
            for status, count in (("ok", args.samples), ("abstain", args.abstain_samples)):
                part = rows[rows.forecast_status == status]
                if len(part):
                    picks.append(part.iloc[np.linspace(0, len(part) - 1, min(count, len(part)), dtype=int)])
            for _, sample in pd.concat(picks).iterrows():
                runtime = forecast_sulfur(args.dataset, str(sample.prediction_origin), horizon_minutes=horizon)
                item = {"origin": str(sample.prediction_origin), "horizon_minutes": int(horizon), "model_fit_end": fit_end,
                        "offline_status": sample.forecast_status, "runtime_status": runtime["status"],
                        "offline_reasons": sample.abstain_reasons if isinstance(sample.abstain_reasons, str) else "",
                        "runtime_reasons": "|".join(runtime["reasons"]), "runtime_fit_end": runtime["model"]["fit_end"]}
                problems = []
                if runtime["status"] != sample.forecast_status:
                    problems.append("status")
                if runtime["status"] == "abstain" and item["offline_reasons"] != item["runtime_reasons"]:
                    problems.append("reasons")
                if runtime["model"]["fit_end"][:10] != str(fit_end)[:10]:
                    problems.append("model_fit_end")
                if runtime["status"] == "ok":
                    for key, offline in (("prediction", sample.prediction), ("prediction_lower", sample.prediction_lower),
                                         ("prediction_upper", sample.prediction_upper), ("exceedance_probability", sample.exceedance_probability)):
                        item[f"offline_{key}"] = float(offline)
                        item[f"runtime_{key}"] = runtime[key]
                        if not np.isclose(runtime[key], offline, rtol=TOLERANCE, atol=TOLERANCE):
                            problems.append(key)
                    if horizon and runtime["stage1"]["status"] != sample.stage1_status:
                        problems.append("stage1_status")
                    if horizon == 0 and not np.isclose(runtime["nowcast"]["prediction"], sample.prediction, rtol=TOLERANCE, atol=TOLERANCE):
                        problems.append("nowcast")
                item["problems"] = problems
                evidence.append(item)
                if problems:
                    failures.append(item)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": not failures, "artifact_sha256": artifact["sha256"], "tolerance": TOLERANCE,
                                       "checked": len(evidence), "failures": failures, "samples": evidence},
                                      indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"passed": not failures, "samples": len(evidence), "failures": len(failures), "output": str(args.output)}))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
