"""Check that runtime forecasts reproduce the offline benchmark predictions.

Offline (``sulfur_forecast.py``) builds features from the full imported
dataset; runtime (``backend/forecast.py``) rebuilds them from a bounded
history window at a single origin.  Both must agree for the same origin and
horizon, otherwise the benchmark metrics do not describe the served model.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()
    artifact = _load_artifact()
    predictions = pd.read_csv(ARTIFACT.parent / "predictions.csv")
    test = predictions[(predictions.split == "test") & (predictions.forecast_status == "ok")]
    evidence = []
    for horizon in artifact["horizons_minutes"]:
        rows = test[test.horizon_minutes == horizon]
        picks = rows.iloc[np.linspace(0, len(rows) - 1, args.samples, dtype=int)] if len(rows) else rows
        for _, sample in picks.iterrows():
            runtime = forecast_sulfur(args.dataset, str(sample.prediction_origin), horizon_minutes=horizon)
            assert runtime["status"] == "ok", (sample.prediction_origin, runtime["reasons"])
            np.testing.assert_allclose(runtime["prediction"], sample.prediction, rtol=1e-6, atol=1e-6)
            np.testing.assert_allclose(runtime["exceedance_probability"], sample.exceedance_probability, rtol=1e-6, atol=1e-6)
            evidence.append({"origin": str(sample.prediction_origin), "horizon_minutes": int(horizon),
                             "offline_prediction": float(sample.prediction), "runtime_prediction": runtime["prediction"],
                             "absolute_error": abs(runtime["prediction"] - sample.prediction)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": True, "artifact_sha256": artifact["sha256"], "samples": evidence},
                                      indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "samples": len(evidence), "output": str(args.output)}))


if __name__ == "__main__":
    main()
