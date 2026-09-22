"""Independent paired review of the v4 candidate; no model promotion or fitting."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ci(delta, dates):
    block = pd.to_datetime(dates).to_numpy(dtype="datetime64[D]").astype("int64") // 7
    grouped = pd.DataFrame({"block": block, "delta": delta}).groupby("block").delta.agg(["sum", "count"])
    ix = np.random.default_rng(42).integers(0, len(grouped), (2000, len(grouped)))
    means = grouped["sum"].to_numpy()[ix].sum(axis=1) / grouped["count"].to_numpy()[ix].sum(axis=1)
    return np.quantile(means, [.025, .975]).tolist()


def run(candidate_root, baseline_root, dataset, output):
    output.mkdir(parents=True, exist_ok=True)
    old_path = baseline_root / "reports/modeling/causal-anchored/corrected-claude/predictions.csv"
    new_path = candidate_root / "reports/modeling/sulfur-forecast/predictions.csv"
    old, new = pd.read_csv(old_path), pd.read_csv(new_path)
    result = {"candidate_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=candidate_root, text=True).strip(),
              "baseline_predictions_sha256": sha(old_path), "candidate_predictions_sha256": sha(new_path),
              "baseline": "2a12b4a / corrected-claude v3", "evaluation": "retrospective 2026, not a new blind holdout",
              "horizons": {}}
    paired = []
    for horizon in (0, 60, 120, 180):
        a = old[(old.horizon_minutes == horizon) & (pd.to_datetime(old.prediction_origin) >= "2026-01-01")]
        b = new[(new.horizon_minutes == horizon) & (new.model_fit_end == "2026-01-01") & (new.split == "walk_forward")]
        m = a.merge(b, on=["prediction_origin", "target_time", "horizon_minutes"], suffixes=("_old", "_new"), validate="one_to_one")
        assert len(m) == len(a) == len(b) == 253
        assert np.allclose(m.target_old, m.target_new)
        rows = m[(m.forecast_status_old == "ok") & (m.forecast_status_new == "ok")].copy()
        errors = {name: abs(rows[f"prediction_{name}"] - rows.target_old) for name in ("old", "new")}
        brier = {name: (rows[f"exceedance_probability_{name}"] - (rows.target_old > 10))**2 for name in ("old", "new")}
        result["horizons"][horizon] = {
            "total": len(m), "common_n": len(rows),
            "accepted_old": int((m.forecast_status_old == "ok").sum()),
            "accepted_new": int((m.forecast_status_new == "ok").sum()),
            "mae_old": float(errors["old"].mean()), "mae_new": float(errors["new"].mean()),
            "mae_delta_weekly_95ci": ci(errors["new"] - errors["old"], rows.target_time),
            "brier_old": float(brier["old"].mean()), "brier_new": float(brier["new"].mean()),
            "brier_delta_weekly_95ci": ci(brier["new"] - brier["old"], rows.target_time),
        }
        paired.append(rows[["horizon_minutes", "prediction_origin", "target_time", "target_old",
                            "prediction_old", "prediction_new", "exceedance_probability_old", "exceedance_probability_new"]])
    pd.concat(paired).to_csv(output / "paired-2026.csv", index=False)
    # Import the reviewed implementation in a fresh interpreter, not the active runtime.
    sys.path.insert(0, str(candidate_root))
    from tools.modeling.sulfur_forecast import DynamicsData, load_dataset, SELECTION_FOLDS
    analysers, controls, labs, _ = load_dataset(dataset)
    dynamics = DynamicsData(analysers, controls)
    crossings = []
    for fold, (start, end) in SELECTION_FOLDS.items():
        for h in (60, 120, 180):
            for analyser in ("q21", "pak"):
                mask = dynamics.rows(analyser, h, "all", between=(pd.Timestamp(start), pd.Timestamp(end)))
                crossing = mask & np.asarray(dynamics.target_time[h] >= pd.Timestamp(end))
                crossings.append({"fold": fold, "horizon": h, "analyser": analyser, "crossing_rows": int(crossing.sum())})
    result["stage1_selection_target_windows_crossing_fold_end"] = crossings
    # Round-trip control move: equal endpoints do not imply no intervention.
    grid = pd.date_range("2025-01-01", periods=3*24*6, freq="10min")
    origin = pd.Timestamp("2025-01-02T12:00")
    temp = pd.Series(350., index=grid)
    temp.loc[(grid > origin) & (grid < origin + pd.Timedelta(hours=1))] = 360.
    synthetic = DynamicsData({"q21": pd.Series(8 + .1*np.sin(np.arange(len(grid))), index=grid)},
                             {"T6": temp, "F9": pd.Series(180., index=grid), "P13": pd.Series(5., index=grid)})
    position = synthetic.origins.get_loc(origin)
    result["quiet_future_roundtrip_counterexample"] = {
        "origin": str(origin), "initial_T6": 350, "intermediate_T6": 360, "final_T6": 350,
        "classified_quiet": bool(synthetic.quiet[60][position]), "expected_quiet": False,
    }
    result["lab_rows"] = len(labs)
    result["observation_path"] = str(dataset / "observations.parquet")
    result["observations_sha256"] = sha(dataset / "observations.parquet")
    (output / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("candidate-root", "baseline-root", "dataset", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    run(args.candidate_root.resolve(), args.baseline_root.resolve(), args.dataset.resolve(), args.output.resolve())
