"""Paired, same-mask comparison of sulphur forecast candidates (protocol v4-repair-2026-09-22).

Candidates on identical origins/horizons and one common accepted mask:

* ``naive_previous_lab`` — last published LIMS (<= 48 h), local level where missing;
* ``anchored_q21`` / ``anchored_pak`` — analyser + median lab offset (bias
  correction), local level where missing;
* ``v3_saved`` — the production v3 artifact's saved predictions: 2024/2025
  ``oof_prediction`` (fitted on earlier years; alpha and quantiles reuse
  2024-2025, so optimistic for v3), 2026 the served model;
* ``v3_refit`` — the v3 recipe (log ridge on the six anchored inputs) refitted
  under the same nested walk-forward protocol as v4r;
* ``v4r`` — repaired two-stage model (``two_stage_forecast.py`` output);
* ``hgb_fixed`` — HistGradientBoosting on the v4r lab-frame inputs, fixed
  hyperparameters, same nested protocol (ablation only).

Outputs: per-candidate ``predictions/*.csv``, ``paired_metrics.csv``,
``paired_ci.csv`` (weekly block bootstrap), ``coverage.csv``,
``abstention.csv`` and ``decision.json`` (the frozen decision rule).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.modeling import anchored_features as v3f  # noqa: E402
from tools.modeling import two_stage_features as tsfeat  # noqa: E402
from tools.modeling import two_stage_forecast as tsf  # noqa: E402

HORIZONS = (0, 60, 120, 180)
LIMIT = 10.0
ALARM = 0.30
PERIODS = {"2024": (2024,), "2025": (2025,), "dev_2024_2025": (2024, 2025), "audit_2026": (2026,)}
BOOTSTRAP = 2000
SEED = 42
HGB_PARAMS = {"max_iter": 300, "learning_rate": 0.05, "max_depth": 3, "min_samples_leaf": 20,
              "l2_regularization": 1.0, "random_state": 0}
V3_ALPHA_GRID = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0, 10000.0, 100000.0)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------
# Generic nested walk-forward family (same rules as two_stage_forecast.fit_procedure)
# ----------------------------------------------------------------------------

class NestedFamily:
    """Configuration chosen on earlier folds only, quantiles from outer out-of-fold predictions."""

    def __init__(self, name, frames, model_columns, support_columns, configs, preregistered, fit, predict, policy):
        self.name, self.frames = name, frames
        self.model_columns, self.support_columns = model_columns, support_columns
        self.configs, self.preregistered = configs, preregistered
        self.fit_fn, self.predict_fn, self.policy = fit, predict, policy
        self.y_ln = {h: tsfeat.ln(f["target"]) for h, f in frames.items()}
        self.available = {h: pd.to_datetime(f["available_at"]) for h, f in frames.items()}
        self.origin = {h: pd.to_datetime(f["prediction_origin"]) for h, f in frames.items()}
        self.cache: dict = {}

    def fit_rows(self, h, cutoff):
        return (self.available[h] < cutoff).to_numpy() & np.isfinite(self.y_ln[h])

    def fold_rows(self, h, start, end):
        return ((self.origin[h] >= start) & (self.available[h] < end)).to_numpy() & np.isfinite(self.y_ln[h])

    def gate(self, h, cutoff):
        key = ("gate", h, cutoff)
        if key not in self.cache:
            rows = self.fit_rows(h, cutoff)
            support = tsf.StandardizedRidge(1.0).fit(self.frames[h].loc[rows, self.support_columns], self.y_ln[h][rows]).artifact()
            support["applicability_policy"] = self.policy
            frame = self.frames[h]
            pairs = np.maximum(frame["anchor_pairs_q21"].to_numpy(), frame["anchor_pairs_pak"].to_numpy()).astype(int)
            checks = [v3f.applicability(r, support, int(p)) if self.policy is v3f.APPLICABILITY_POLICY
                      else tsfeat.applicability(r, support, int(p))
                      for r, p in zip(frame[self.support_columns].to_numpy(dtype=float), pairs)]
            self.cache[key] = (np.array([c["status"] == "ok" for c in checks]), ["|".join(c["reasons"]) for c in checks])
        return self.cache[key]

    def procedure(self, h, cutoff, folds):
        key = ("proc", h, cutoff, tuple(folds.items()))
        if key in self.cache:
            return self.cache[key]
        frame, y = self.frames[h], self.y_ln[h]
        outer = {n: self.procedure(h, s, {k: v for k, v in folds.items() if v[1] <= s}) for n, (s, _) in folds.items()}
        if not folds:
            config, selection = self.preregistered, {"basis": "preregistered"}
        elif len(self.configs) == 1:
            config, selection = self.configs[0], {"basis": "fixed"}
        else:
            scores = {}
            for config in self.configs:
                per_fold = []
                for n, (s, e) in folds.items():
                    ok, _ = self.gate(h, s)
                    fit, score = self.fit_rows(h, s) & ok, self.fold_rows(h, s, e) & ok
                    if fit.sum() < tsf.MIN_SUPPORT_ROWS or not score.any():
                        continue
                    model = self.fit_fn(config, frame.loc[fit, self.model_columns], y[fit])
                    per_fold.append(float(np.mean(np.abs(y[score] - self.predict_fn(model, frame.loc[score, self.model_columns])))))
                scores[config] = float(np.mean(per_fold)) if per_fold else np.inf
            config = min(scores, key=scores.get)
            selection = {"basis": "earlier_folds", "scores": {str(k): v for k, v in scores.items()}}
        ok, _ = self.gate(h, cutoff)
        fit = self.fit_rows(h, cutoff) & ok
        model = self.fit_fn(config, frame.loc[fit, self.model_columns], y[fit])
        residuals = []
        for n, (s, e) in folds.items():
            pred = self.predict(h, outer[n], self.fold_rows(h, s, e))
            accepted = pred["forecast_status"].eq("ok")
            residuals.append((tsfeat.ln(pred["target"]) - pred["ln_prediction"])[accepted])
        quantiles = tsfeat.residual_quantile_function(np.concatenate(residuals)) if residuals else None
        result = {"cutoff": cutoff, "config": config, "selection": selection, "model": model, "quantiles": quantiles}
        self.cache[key] = result
        return result

    def predict(self, h, proc, rows):
        frame = self.frames[h]
        index = np.flatnonzero(rows)
        ok, reasons = self.gate(h, proc["cutoff"])
        ln_pred = self.predict_fn(proc["model"], frame.iloc[index][self.model_columns])
        status = np.where(ok[index] & np.isfinite(ln_pred) & (ln_pred < 50), "ok", "abstain")
        q = proc["quantiles"]
        out = pd.DataFrame({"horizon_minutes": h, "prediction_origin": frame["prediction_origin"].to_numpy()[index],
                            "target": frame["target"].to_numpy(dtype=float)[index], "forecast_status": status,
                            "abstain_reasons": [reasons[i] for i in index], "ln_prediction": ln_pred,
                            "prediction": np.exp(ln_pred), "model_cutoff": proc["cutoff"].isoformat(),
                            "config": str(proc["config"])}, index=index)
        if q is not None:
            out["prediction_lower"] = [tsfeat.interval(q, v)[0] if np.isfinite(v) else np.nan for v in ln_pred]
            out["prediction_upper"] = [tsfeat.interval(q, v)[1] if np.isfinite(v) else np.nan for v in ln_pred]
            out["exceedance_probability"] = [tsfeat.exceedance_probability(q, v, np.log(LIMIT)) if np.isfinite(v) else np.nan
                                             for v in ln_pred]
        return out

    def walk_forward(self, fit_ends, folds):
        parts = []
        for i, fit_end in enumerate(fit_ends):
            nxt = fit_ends[i + 1] if i + 1 < len(fit_ends) else None
            fit_folds = {k: v for k, v in folds.items() if v[1] <= fit_end}
            for h in HORIZONS:
                proc = self.procedure(h, fit_end, fit_folds)
                window = (self.origin[h] >= fit_end) & ((self.origin[h] < nxt) if nxt is not None else True)
                pred = self.predict(h, proc, window.to_numpy() & np.isfinite(self.y_ln[h]))
                pred["model_fit_end"] = fit_end.isoformat()
                parts.append(pred)
        return pd.concat(parts, ignore_index=True)


def _ridge_fit(alpha, x, y):
    return tsf.StandardizedRidge(alpha).fit(x, y)


def _ridge_predict(model, x):
    return model.predict(x)


def _hgb_fit(_config, x, y):
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(**HGB_PARAMS).fit(x.to_numpy(dtype=float), y)


def _hgb_predict(model, x):
    return model.predict(x.to_numpy(dtype=float))


def v3_frames(dataset: Path) -> dict[int, pd.DataFrame]:
    analysers, controls, labs, _, _ = tsf.load_dataset(dataset)
    delay = pd.Timedelta(minutes=v3f.LIMS_PUBLICATION_DELAY_MINUTES)
    frames = {}
    for h in HORIZONS:
        origins = pd.DatetimeIndex(labs["target_time"]) - pd.Timedelta(minutes=h)
        frame = v3f.build_features(analysers, controls, labs, origins, v3f.LIMS_PUBLICATION_DELAY_MINUTES)
        frame["target_time"] = labs["target_time"].to_numpy()
        frame["target"] = labs["target"].to_numpy(dtype=float)
        frame["available_at"] = frame["target_time"] + delay
        frames[h] = frame.reset_index(drop=True)
    return frames


def v4r_frames(dataset: Path) -> dict[int, pd.DataFrame]:
    analysers, controls, labs, _, invalid = tsf.load_dataset(dataset)
    return tsf.build_context(analysers, controls, labs, tsf.LIMS_PUBLICATION_DELAY_MINUTES, invalid).lab_frames


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------

def metrics(y, pred, prob=None, lower=None, upper=None) -> dict:
    y, pred = np.asarray(y, float), np.asarray(pred, float)
    err = pred - y
    danger, alarm = y > LIMIT, pred > LIMIT
    tp, fp, fn = int((danger & alarm).sum()), int((~danger & alarm).sum()), int((danger & ~alarm).sum())
    out = {"n": int(len(y)), "exceedances": int(danger.sum()), "mae": float(np.mean(np.abs(err))),
           "rmse": float(np.sqrt(np.mean(err ** 2))), "median_ae": float(np.median(np.abs(err))), "bias": float(np.mean(err)),
           "point_recall": tp / (tp + fn) if tp + fn else None, "point_precision": tp / (tp + fp) if tp + fp else None,
           "point_fpr": fp / max(1, int((~danger).sum()))}
    if prob is not None and np.isfinite(np.asarray(prob, float)).all():
        prob = np.asarray(prob, float)
        base = danger.mean()
        brier = float(np.mean((prob - danger) ** 2))
        clim = float(np.mean((base - danger) ** 2))
        auc = None
        if danger.any() and (~danger).any():
            ranks = pd.Series(prob).rank().to_numpy()
            auc = float((ranks[danger].sum() - danger.sum() * (danger.sum() + 1) / 2) / (danger.sum() * (~danger).sum()))
        a = prob >= ALARM
        tp, fp, fn = int((danger & a).sum()), int((~danger & a).sum()), int((danger & ~a).sum())
        out.update({"brier": brier, "brier_skill": 1 - brier / clim if clim > 0 else None, "auc": auc,
                    "prob_recall": tp / (tp + fn) if tp + fn else None, "prob_precision": tp / (tp + fp) if tp + fp else None,
                    "prob_fpr": fp / max(1, int((~danger).sum())), "alarms": int(a.sum())})
    if lower is not None and np.isfinite(np.asarray(lower, float)).all():
        out["interval80_coverage"] = float(np.mean((y >= np.asarray(lower, float)) & (y <= np.asarray(upper, float))))
    return out


def weekly_ci(delta, times) -> list[float]:
    block = pd.to_datetime(times).to_numpy(dtype="datetime64[D]").astype("int64") // 7
    grouped = pd.DataFrame({"block": block, "delta": np.asarray(delta, float)}).groupby("block").delta.agg(["sum", "count"])
    ix = np.random.default_rng(SEED).integers(0, len(grouped), (BOOTSTRAP, len(grouped)))
    means = grouped["sum"].to_numpy()[ix].sum(axis=1) / grouped["count"].to_numpy()[ix].sum(axis=1)
    return [float(v) for v in np.quantile(means, [0.025, 0.975])]


# ----------------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------------

def run(dataset: Path, v4r_dir: Path, v3_dir: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    (output / "predictions").mkdir(exist_ok=True)
    fit_ends = [pd.Timestamp(e) for e in tsf.WALK_FORWARD_FIT_ENDS]
    folds = {k: (pd.Timestamp(s), pd.Timestamp(e)) for k, (s, e) in tsf.SELECTION_FOLDS.items()}
    key = ["horizon_minutes", "prediction_origin"]

    v4r = pd.read_csv(v4r_dir / "predictions.csv", parse_dates=["prediction_origin", "target_time"])
    v4r = v4r[v4r.split.eq("walk_forward")].copy()
    candidates: dict[str, pd.DataFrame] = {
        "v4r": v4r[key + ["target_time", "target", "forecast_status", "abstain_reasons", "prediction", "prediction_lower",
                          "prediction_upper", "exceedance_probability", "model_fit_end"]].copy()}
    for name, column in (("naive_previous_lab", "baseline_previous_lab"), ("anchored_q21", "baseline_q21_anchored"),
                         ("anchored_pak", "baseline_pak_anchored")):
        value = v4r[column].to_numpy(float)
        substituted = ~np.isfinite(value)
        candidates[name] = v4r[key + ["target_time", "target"]].assign(
            prediction=np.where(substituted, v4r["baseline_level"], value), level_substituted=substituted,
            forecast_status=np.where(np.isfinite(np.where(substituted, v4r["baseline_level"], value)), "ok", "abstain"))

    saved = pd.read_csv(v3_dir / "predictions.csv", parse_dates=["prediction_origin", "target_time"])
    saved = saved[saved.split.isin(["selection_2024", "selection_2025", "test"])].copy()
    dev = saved.split.ne("test")
    candidates["v3_saved"] = pd.DataFrame({
        "horizon_minutes": saved.horizon_minutes, "prediction_origin": saved.prediction_origin, "target_time": saved.target_time,
        "target": saved.target, "forecast_status": saved.forecast_status, "abstain_reasons": saved.abstain_reasons,
        "prediction": np.where(dev, saved.oof_prediction, saved.prediction),
        "prediction_lower": np.where(dev, np.nan, saved.prediction_lower),
        "prediction_upper": np.where(dev, np.nan, saved.prediction_upper),
        "exceedance_probability": np.where(dev, saved.oof_exceedance_probability, saved.exceedance_probability),
        "source_column": np.where(dev, "oof_prediction (fit on earlier years)", "prediction (served model, fit < 2026)")})

    print("v3 refit (nested)", file=sys.stderr)
    frames3 = v3_frames(dataset)
    family = NestedFamily("v3_refit", frames3, v3f.MODEL_COLUMNS, v3f.FEATURE_COLUMNS, list(V3_ALPHA_GRID), 30.0,
                          _ridge_fit, _ridge_predict, v3f.APPLICABILITY_POLICY)
    refit = family.walk_forward(fit_ends, folds)
    refit["prediction_origin"] = pd.to_datetime(refit["prediction_origin"])
    candidates["v3_refit"] = refit
    print("hgb fixed (nested)", file=sys.stderr)
    frames4 = v4r_frames(dataset)
    hgb_columns = list(dict.fromkeys(tsfeat.SUPPORT_COLUMNS + tsfeat.DYNAMICS_COLUMNS))
    hgb = NestedFamily("hgb_fixed", frames4, hgb_columns, tsfeat.SUPPORT_COLUMNS, ["fixed"], "fixed",
                       _hgb_fit, _hgb_predict, tsfeat.APPLICABILITY_POLICY).walk_forward(fit_ends, folds)
    hgb["prediction_origin"] = pd.to_datetime(hgb["prediction_origin"])
    candidates["hgb_fixed"] = hgb
    configs = {"v3_refit": {f"{e.date()}/{h}": str(family.procedure(h, e, {k: v for k, v in folds.items() if v[1] <= e})["config"])
                            for e in fit_ends for h in HORIZONS}}

    # ---- merge on identical origins
    base = candidates["v4r"][key + ["target_time", "target"]].copy()
    base["year"] = pd.to_datetime(base["target_time"]).dt.year
    merged = base.copy()
    for name, frame in candidates.items():
        frame = frame.drop_duplicates(key)
        cols = [c for c in ("prediction", "prediction_lower", "prediction_upper", "exceedance_probability", "forecast_status",
                            "abstain_reasons", "level_substituted", "target") if c in frame]
        merged = merged.merge(frame[key + cols].rename(columns={c: f"{name}__{c}" for c in cols}), on=key, how="left",
                              validate="one_to_one")
        other = merged[f"{name}__target"]
        both = other.notna()
        if not np.allclose(other[both], merged.loc[both, "target"]):
            raise AssertionError(f"target mismatch for {name}")
        frame.to_csv(output / "predictions" / f"{name}.csv", index=False)
    merged["common"] = np.isfinite(merged["target"])
    for name in ("v3_saved", "v4r", "v3_refit"):
        merged["common"] &= merged[f"{name}__forecast_status"].eq("ok")
    for name in ("hgb_fixed", "naive_previous_lab", "anchored_q21", "anchored_pak"):
        merged["common"] &= merged[f"{name}__forecast_status"].eq("ok")
    merged.to_csv(output / "paired_rows.csv", index=False)

    # ---- own coverage
    coverage_rows = []
    for name in candidates:
        for period, years in PERIODS.items():
            for h in HORIZONS:
                part = merged[merged.year.isin(years) & merged.horizon_minutes.eq(h) & np.isfinite(merged.target)]
                if not len(part):
                    continue
                ok = part[f"{name}__forecast_status"].eq("ok")
                row = {"candidate": name, "period": period, "horizon": h, "rows": len(part), "accepted": int(ok.sum()),
                       "coverage": float(ok.mean()), "abstained_above_10": int((~ok & (part.target > LIMIT)).sum()),
                       "common_rows": int(part.common.sum())}
                own = part[ok]
                prob = own.get(f"{name}__exceedance_probability")
                row.update({f"own_{k}": v for k, v in metrics(own.target, own[f"{name}__prediction"],
                                                                prob if prob is not None else None).items()
                            if k in ("mae", "rmse", "brier", "auc")})
                coverage_rows.append(row)
    pd.DataFrame(coverage_rows).to_csv(output / "coverage.csv", index=False)

    # ---- abstention reasons on the audit/dev windows
    reasons = []
    for name in ("v4r", "v3_saved", "v3_refit"):
        col = f"{name}__abstain_reasons"
        part = merged[merged[f"{name}__forecast_status"].ne("ok")]
        for (period, h), grp in part.groupby([part.year, part.horizon_minutes]):
            for reason, count in grp[col].fillna("missing_prediction").str.split("|").explode().value_counts().items():
                reasons.append({"candidate": name, "year": period, "horizon": h, "reason": reason or "unspecified", "rows": int(count)})
    pd.DataFrame(reasons).to_csv(output / "abstention.csv", index=False)

    # ---- paired metrics and CI on the common mask
    rows, ci_rows = [], []
    probabilistic = ("v3_saved", "v3_refit", "v4r", "hgb_fixed")
    for period, years in PERIODS.items():
        for h in HORIZONS:
            part = merged[merged.common & merged.year.isin(years) & merged.horizon_minutes.eq(h)]
            if not len(part):
                continue
            for name in candidates:
                prob = part[f"{name}__exceedance_probability"] if name in probabilistic else None
                lower = part.get(f"{name}__prediction_lower")
                m = metrics(part.target, part[f"{name}__prediction"], prob,
                            lower, part.get(f"{name}__prediction_upper"))
                sub = part.get(f"{name}__level_substituted")
                m.update({"candidate": name, "period": period, "horizon": h,
                          "level_substituted_fraction": float(sub.mean()) if sub is not None else None})
                rows.append(m)
            error = {n: (part[f"{n}__prediction"] - part.target).abs() for n in candidates}
            danger = (part.target > LIMIT).astype(float)
            for name in candidates:
                for ref in ("v3_saved", "v3_refit", "naive_previous_lab"):
                    if name == ref:
                        continue
                    entry = {"period": period, "horizon": h, "candidate": name, "reference": ref, "n": len(part),
                             "mae_delta": float((error[name] - error[ref]).mean()),
                             "mae_delta_95ci": weekly_ci(error[name] - error[ref], part.target_time),
                             "mae_reference": float(error[ref].mean())}
                    if name in probabilistic and ref in probabilistic:
                        b = (part[f"{name}__exceedance_probability"] - danger) ** 2 - (part[f"{ref}__exceedance_probability"] - danger) ** 2
                        entry.update({"brier_delta": float(b.mean()), "brier_delta_95ci": weekly_ci(b, part.target_time)})
                    ci_rows.append(entry)
    paired = pd.DataFrame(rows)
    first = ["candidate", "period", "horizon"]
    paired = paired[first + [c for c in paired.columns if c not in first]]
    paired.to_csv(output / "paired_metrics.csv", index=False)
    ci = pd.DataFrame(ci_rows)
    ci.to_csv(output / "paired_ci.csv", index=False)

    decision = {ref: decision_rule(paired, ci, ref) for ref in ("v3_saved", "v3_refit")}
    summary = {
        "protocol": "reports/review/v4-repair-2026-09-22/PROTOCOL.md",
        "dataset_sha256": sha256(dataset / "observations.parquet"),
        "v4r_model_sha256": sha256(v4r_dir / "model.json"), "v4r_predictions_sha256": sha256(v4r_dir / "predictions.csv"),
        "v3_model_sha256": sha256(v3_dir / "model.json"), "v3_predictions_sha256": sha256(v3_dir / "predictions.csv"),
        "common_rows": {p: {h: int((merged.common & merged.year.isin(y) & merged.horizon_minutes.eq(h)).sum()) for h in HORIZONS}
                        for p, y in PERIODS.items()},
        "hgb_params": HGB_PARAMS, "v3_refit_configs": configs, "bootstrap": {"replicates": BOOTSTRAP, "seed": SEED, "block": "7 days"},
        "decision_rule": decision,
    }
    (output / "decision.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def decision_rule(paired: pd.DataFrame, ci: pd.DataFrame, reference: str) -> dict:
    """The frozen rule of the protocol on the pooled development years."""
    checks = []
    dev = paired[paired.period.eq("dev_2024_2025")]
    dci = ci[ci.period.eq("dev_2024_2025") & ci.candidate.eq("v4r") & ci.reference.eq(reference)]
    for h in HORIZONS:
        row = dci[dci.horizon.eq(h)].iloc[0]
        upper = row["mae_delta_95ci"][1]
        if h in (0, 60):
            checks.append({"horizon": h, "check": "MAE CI upper < 0", "value": upper, "passed": bool(upper < 0)})
        else:
            bound = 0.02 * row["mae_reference"]
            checks.append({"horizon": h, "check": "MAE CI upper < 2% of reference MAE", "value": upper, "bound": bound,
                           "passed": bool(upper < bound)})
        a = dev[dev.candidate.eq("v4r") & dev.horizon.eq(h)].iloc[0]
        b = dev[dev.candidate.eq(reference) & dev.horizon.eq(h)].iloc[0]
        checks.append({"horizon": h, "check": "Brier <= reference + 0.005", "value": a["brier"], "reference": b["brier"],
                       "passed": bool(a["brier"] <= b["brier"] + 0.005)})
        if a["exceedances"] >= 5:
            checks.append({"horizon": h, "check": "recall at P>=0.30 not lower", "value": a["prob_recall"],
                           "reference": b["prob_recall"], "passed": bool(a["prob_recall"] >= b["prob_recall"])})
    return {"reference": reference, "passed": all(c["passed"] for c in checks), "checks": checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--v4r", type=Path, default=ROOT / "reports/modeling/two-stage-v4r")
    parser.add_argument("--v3", type=Path, default=ROOT / "reports/modeling/causal-anchored/corrected-claude")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run(args.dataset.resolve(), args.v4r.resolve(), args.v3.resolve(), args.output.resolve())
    print(json.dumps({"common_rows": summary["common_rows"],
                      "decision": {k: v["passed"] for k, v in summary["decision_rule"].items()}}, indent=2))


if __name__ == "__main__":
    main()
