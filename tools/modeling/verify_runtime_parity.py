"""Compare source-file features/benchmark predictions with imported runtime data."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.forecast import _load_artifact, _runtime_features, forecast_sulfur
from tools.modeling.sulfur_features import available_lab, telemetry_features
from tools.modeling.sulfur_first_iteration import load_lab_target, load_telemetry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = _load_artifact()
    telemetry = load_telemetry(args.source)
    labs = load_lab_target(args.source)
    predictions = pd.read_csv(ROOT / "reports/modeling/sulfur-first-iteration/predictions.csv")
    samples = predictions[(predictions.split == "test") & (predictions.forecast_status == "ok")].iloc[[0, 3, 20, 60, 120, -1]]
    evidence = []
    for _, sample in samples.iterrows():
        origin = pd.Timestamp(sample.prediction_origin)
        origins = pd.DatetimeIndex([origin])
        offline, _ = telemetry_features(telemetry, origins)
        offline["previous_lab_available"] = available_lab(labs, origins)["previous_lab_available"]
        expected = offline.loc[0, artifact["feature_columns"]].to_numpy(dtype=float)
        actual, _, _ = _runtime_features(args.dataset, origin, artifact)
        np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-5, equal_nan=True)
        runtime = forecast_sulfur(args.dataset, origin.isoformat())
        assert runtime["status"] == "ok", runtime["reasons"]
        np.testing.assert_allclose(runtime["prediction_ridge"], sample.prediction_ridge, rtol=1e-6, atol=1e-5)
        evidence.append({"origin": origin.isoformat(), "max_feature_absolute_error": float(np.nanmax(np.abs(actual-expected))),
                         "prediction_absolute_error": abs(runtime["prediction_ridge"]-sample.prediction_ridge)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": True, "artifact_sha256": artifact["sha256"], "samples": evidence}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "samples": len(evidence), "output": str(args.output)}))


if __name__ == "__main__":
    main()
