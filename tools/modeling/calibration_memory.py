"""Pre-2026-only search of additive calibration memory; frozen second protocol."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.anchored_challengers import fit_candidate, predict, gate
from tools.modeling.sulfur_forecast import load_dataset, temporal_folds, regression_metrics, sha256
from tools.modeling.anchored_features import build_features, HORIZONS_MINUTES


def run(dataset, output):
    analysers, controls, labs = load_dataset(dataset)
    # Search cannot even see target labels or analyzer readings from 2026.
    labs = labs[labs.target_time + pd.Timedelta(hours=4) < pd.Timestamp("2026-01-01")].reset_index(drop=True)
    analysers = {k: v[v.index < pd.Timestamp("2026-01-01")] for k, v in analysers.items()}
    controls = {k: v[v.index < pd.Timestamp("2026-01-01")] for k, v in controls.items()}
    choices = [(a, l) for a in (3, 5, 10, 20) for l in (3, 5, 10)]
    scores = {str(pair): {} for pair in choices}
    for h in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs.target_time) - pd.Timedelta(minutes=h)
        y = labs.target.to_numpy(float)
        data = {}
        for pair in choices:
            frame = build_features(analysers, controls, labs, origins, anchor_samples=pair[0], level_samples=pair[1])
            data[pair] = {}
            for year in (2024, 2025):
                fit = (labs.target_time + pd.Timedelta(hours=4) < pd.Timestamp(f"{year}-01-01")).to_numpy()
                score = (origins >= pd.Timestamp(f"{year}-01-01")) & (labs.target_time + pd.Timedelta(hours=4) < pd.Timestamp(f"{year+1}-01-01"))
                _, ok, _ = gate(frame, fit, y)
                model = fit_candidate("ridge_log_100", frame.loc[fit], y[fit])
                data[pair][year] = (np.exp(predict(model, frame)), ok & np.asarray(score))
        for pair in choices:
            scores[str(pair)][str(h)] = {}
            for year in (2024, 2025):
                p, ok = data[pair][year]
                base, base_ok = data[(10, 5)][year]
                common = ok & base_ok
                scores[str(pair)][str(h)][str(year)] = {
                    "candidate": regression_metrics(y[common], p[common]),
                    "incumbent": regression_metrics(y[common], base[common]),
                    "candidate_accepted": int(ok.sum()), "incumbent_accepted": int(base_ok.sum()),
                }
        print(f"calibration search completed H{h}", flush=True)
    def ratio(pair, year, metric):
        rows = scores[str(pair)]
        return float(sum(rows[str(h)][str(year)]["candidate"][metric] for h in HORIZONS_MINUTES) /
                     sum(rows[str(h)][str(year)]["incumbent"][metric] for h in HORIZONS_MINUTES))
    winner = min(choices, key=lambda p: np.mean([scores[str(p)][str(h)]["2024"]["candidate"]["mae"] for h in HORIZONS_MINUTES]))
    promoted = ratio(winner, 2025, "mae") <= .98 and ratio(winner, 2025, "rmse") <= 1.05
    result = {"winner": winner, "selected": winner if promoted else (10, 5), "promoted": bool(promoted),
              "winner_2024_mae_ratio": ratio(winner, 2024, "mae"),
              "winner_2025_mae_ratio": ratio(winner, 2025, "mae"),
              "winner_2025_rmse_ratio": ratio(winner, 2025, "rmse"), "scores": scores,
              "observations_sha256": sha256(dataset / "observations.parquet"),
              "script_sha256": sha256(Path(__file__)),
              "protocol_sha256": sha256(output / "CALIBRATION_PROTOCOL.md")}
    (output / "selection.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps({k: v for k, v in result.items() if k != "scores"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/modeling/causal-anchored")
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        run(args.dataset, args.output)
