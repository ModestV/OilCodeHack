"""Frozen temporal comparison of robust sensor fusion; see PROTOCOL.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye, hstack
from sklearn.linear_model import HuberRegressor
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.modeling.sulfur_forecast import (
    StandardizedRidge, load_dataset, temporal_folds, regression_metrics,
    probability_metrics, sha256, _json_default,
)
from tools.modeling.anchored_features import (
    MODEL_COLUMNS, FEATURE_COLUMNS, HORIZONS_MINUTES, APPLICABILITY_POLICY,
    build_features, applicability, ln, residual_quantile_function,
    interval, exceedance_probability, predict_portable,
)

CANDIDATES = [*(f"ridge_log_{a}" for a in (10, 100, 1000, 10000)),
              "huber_log", "huber_raw", "convex_log", "convex_raw",
              "q21", "pak", "previous_lab", "level"]


def fit_candidate(name, frame, target):
    """Export every model as a small affine artifact, without pickle/sklearn in runtime."""
    raw = name.endswith("raw") or name in ("q21", "pak", "previous_lab", "level")
    inputs = np.exp(frame[MODEL_COLUMNS]) if raw else frame[MODEL_COLUMNS]
    y = np.asarray(target, float) if raw else ln(target)
    base = StandardizedRidge(10).fit(inputs, y)
    artifact = base.artifact()
    artifact.update(algorithm=name, input_transform="exp" if raw else "identity",
                    output_transform="log" if raw else "identity")
    values = inputs.to_numpy(float)
    values = np.where(np.isfinite(values), values, base.medians_)
    if name.startswith("ridge"):
        artifact.update(StandardizedRidge(float(name.rsplit("_", 1)[1])).fit(inputs, y).artifact())
    elif name.startswith("huber"):
        z = (values - base.mean_) / base.scale_
        model = HuberRegressor(epsilon=1.35, alpha=1, max_iter=5000).fit(z, y)
        artifact["coef"] = [float(model.intercept_), *model.coef_.tolist()]
    elif name.startswith("convex"):
        # Min sum |y-Xw|, w>=0, sum w=1. L1 tolerates lab outliers without deleting them.
        n, p = values.shape
        matrix = csr_matrix(values)
        constraints = hstack([matrix, -eye(n)]).tocsr()
        opposite = hstack([-matrix, -eye(n)]).tocsr()
        from scipy.sparse import vstack
        result = linprog(np.r_[np.zeros(p), np.ones(n) / n],
                         A_ub=vstack([constraints, opposite]), b_ub=np.r_[y, -y],
                         A_eq=csr_matrix(np.r_[np.ones(p), np.zeros(n)][None, :]),
                         b_eq=[1], bounds=(0, None), method="highs")
        if not result.success:
            raise RuntimeError(result.message)
        artifact.update(mean=[0.] * p, scale=[1.] * p, coef=[0., *result.x[:p].tolist()])
    else:
        col = {"q21": "ln_q21", "pak": "ln_pak", "level": "ln_level",
               "previous_lab": "ln_previous_lab"}[name]
        artifact.update(baseline_column=col, fallback_level=True,
                        fallback_median=float(np.median(target)))
    return artifact


def predict(model, frame):
    return np.array([predict_portable(model, row) for row in frame[MODEL_COLUMNS].to_numpy(float)])


def gate(frame, fit, y):
    support = StandardizedRidge(10).fit(frame.loc[fit, FEATURE_COLUMNS], ln(y[fit])).artifact()
    rows = [applicability(row, {**support, "applicability_policy": APPLICABILITY_POLICY},
                          int(max(frame.loc[i, "anchor_pairs_q21"], frame.loc[i, "anchor_pairs_pak"])))
            for i, row in enumerate(frame[FEATURE_COLUMNS].to_numpy(float))]
    return support, np.array([r["status"] == "ok" for r in rows]), rows


def paired_ci(frame, selected, baseline):
    differences = np.abs(selected - frame.target.to_numpy()) - np.abs(baseline - frame.target.to_numpy())
    blocks = pd.DataFrame({"day": pd.to_datetime(frame.target_time).dt.date, "delta": differences})
    groups = [part.delta.to_numpy() for _, part in blocks.groupby("day")]
    rng = np.random.default_rng(42)
    means = [np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))]).mean() for _ in range(3000)]
    return np.quantile(means, [.025, .975]).tolist()


def run(dataset, output, corrected):
    output.mkdir(parents=True, exist_ok=True)
    analysers, controls, labs = load_dataset(dataset)
    old = pd.read_csv(corrected / "predictions.csv")
    artifact = json.loads((corrected / "model.json").read_text(encoding="utf8"))
    artifact.update(model="causal lab-anchored robust sensor fusion; frozen temporal selection",
                    training_protocol="2024 selection; 2025 confirmation/calibration; 2026 retrospective audit",
                    calibration_note="Accepted 2025 out-of-time residuals; 2024 model selection and 2025 promotion check; fixed alarm threshold .30",
                    alarm_policy="fixed probability threshold 0.30; not optimized on calibration data")
    summaries, predictions = {}, []
    for h in HORIZONS_MINUTES:
        origins = pd.DatetimeIndex(labs.target_time) - pd.Timedelta(minutes=h)
        frame = build_features(analysers, controls, labs, origins)
        frame["target_time"], frame["target"] = labs.target_time, labs.target
        y = labs.target.to_numpy(float)
        folds = temporal_folds(frame.target_time, frame.prediction_origin, pd.Timedelta(hours=4))
        fits, preds, stats, gates = {}, {}, {}, {}
        for fold in ("2024", "2025", "final"):
            fit, score = folds[fold]
            support, ok, reasons = gate(frame, fit, y)
            gates[fold] = (support, ok, reasons)
            accepted = score.to_numpy() & ok
            fits[fold], preds[fold], stats[fold] = {}, {}, {}
            for name in CANDIDATES:
                model = fit_candidate(name, frame.loc[fit], y[fit])
                p = predict(model, frame)
                fits[fold][name], preds[fold][name] = model, p
                stats[fold][name] = {
                    "accepted": regression_metrics(y[accepted], np.exp(p[accepted])),
                    "all": regression_metrics(y[score], np.exp(p[score])),
                    "accepted_log_mae": float(np.mean(np.abs(ln(y[accepted]) - p[accepted]))),
                }
        winner = min(CANDIDATES, key=lambda k: stats["2024"][k]["accepted"]["mae"])
        incumbent = min(CANDIDATES[:4], key=lambda k: stats["2024"][k]["accepted_log_mae"])
        w, b = stats["2025"][winner]["accepted"], stats["2025"][incumbent]["accepted"]
        promoted = w["mae"] <= .98 * b["mae"] and w["rmse"] <= 1.05 * b["rmse"]
        selected = winner if promoted else incumbent
        fit, test = folds["final"]
        support, ok, reasons = gates["final"]
        calibration = folds["2025"][1].to_numpy() & gates["2025"][1]
        residual = ln(y[calibration]) - preds["2025"][selected][calibration]
        quantiles = residual_quantile_function(residual)
        final_model = {**fits["final"][selected], "support": support,
                       "residual_quantiles": quantiles, "alarm_probability": .30}
        artifact["models"][str(h)] = final_model
        prediction = preds["final"][selected]
        probability = np.array([exceedance_probability(quantiles, v, np.log(10)) for v in prediction])
        intervals = np.array([interval(quantiles, v) for v in prediction])
        accepted = test.to_numpy() & ok
        corrected_h = old[old.horizon_minutes == h].reset_index(drop=True)
        assert np.array_equal(corrected_h.forecast_status.eq("ok"), ok)
        assert np.allclose(corrected_h.target, y)
        common = accepted & frame[MODEL_COLUMNS].notna().all(axis=1).to_numpy()
        summaries[str(h)] = {
            "winner_2024": winner, "incumbent_2024": incumbent, "promoted": bool(promoted), "selected": selected,
            "fold_scores": stats, "audit_rows": int(test.sum()), "accepted_rows": int(accepted.sum()),
            "calibration_rows": int(calibration.sum()), "calibration_last_label_available": str((frame.loc[calibration, "target_time"] + pd.Timedelta(hours=4)).max()),
            "accepted": regression_metrics(y[accepted], np.exp(prediction[accepted])),
            "corrected_claude": regression_metrics(y[accepted], corrected_h.loc[accepted, "prediction"]),
            "incumbent": stats["final"][incumbent]["accepted"],
            "paired_delta_mae_ci95_vs_corrected": paired_ci(frame.loc[accepted], np.exp(prediction[accepted]), corrected_h.loc[accepted, "prediction"].to_numpy()),
            "complete_sensor_rows": int(common.sum()),
            "complete_sensor_metrics": {name: regression_metrics(y[common], np.exp(preds["final"][name][common])) for name in CANDIDATES},
            "probability": probability_metrics(y[accepted], probability[accepted], .30),
            "interval80_coverage": float(((y >= intervals[:, 0]) & (y <= intervals[:, 1]))[accepted].mean()),
            "interval80_mean_width": float((intervals[:, 1] - intervals[:, 0])[accepted].mean()),
        }
        frame["horizon_minutes"] = h
        frame["split"] = np.where(test, "audit_2026", "pre_2026")
        frame["forecast_status"] = np.where(ok, "ok", "abstain")
        frame["abstain_reasons"] = ["|".join(r["reasons"]) for r in reasons]
        frame["prediction"] = np.exp(prediction)
        frame["prediction_lower"], frame["prediction_upper"] = intervals[:, 0], intervals[:, 1]
        frame["exceedance_probability"] = probability
        frame["corrected_claude_prediction"] = corrected_h.prediction
        for name in CANDIDATES:
            frame[name] = np.exp(preds["final"][name])
        predictions.append(frame)
        print(json.dumps({"horizon": h, "selected": selected, "winner": winner, "promoted": bool(promoted),
                          "mae": summaries[str(h)]["accepted"]["mae"],
                          "corrected_mae": summaries[str(h)]["corrected_claude"]["mae"]}), flush=True)
    (output / "model.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf8")
    pd.concat(predictions, ignore_index=True).to_csv(output / "predictions.csv", index=False)
    summary = {"horizons": summaries, "observations_sha256": sha256(dataset / "observations.parquet"),
               "model_sha256": sha256(output / "model.json"), "script_sha256": sha256(Path(__file__)),
               "protocol_sha256": sha256(output / "PROTOCOL.md"), "candidates": CANDIDATES}
    (output / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/modeling/causal-anchored")
    parser.add_argument("--corrected", type=Path, default=ROOT / "reports/modeling/causal-anchored/corrected-claude")
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        run(args.dataset, args.output, args.corrected)
