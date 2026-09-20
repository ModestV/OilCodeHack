"""Historical backtest of the agent contour and of the VAK soft sensors.

Part 1 — decision contour.  For every hydro-treating sulphur lab sample in
the evaluation period the contour is executed exactly as the API would run it
at ``origin = sample_time - horizon`` (default 180 minutes), with only data
known at the origin.  The forecast, the nowcast at the sampling time, the
``hold`` candidate and the final decision are compared with the laboratory
result that arrives later.  Nothing here validates the effect of a
recommended change (there is no counterfactual history); the backtest checks
whether the contour sees the right level, raises alarms before real
exceedances, and abstains for the right reasons.

Part 2 — VAK formulas.  Each corrected formula is evaluated at the sampling
times of every LIMS series with the same parameter and the error statistics
are reported.  For AVT the organisers gave no explicit point↔fraction map, so
every candidate point is scored and the best match is reported as a
hypothesis, not as a confirmed mapping.

Example::

    python tools/modeling/backtest_agents.py --dataset storage/hackathon \
        --output reports/verification/agent-backtest --start 2026-01-01
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agents import make_decision  # noqa: E402
from backend.config import REGISTRY  # noqa: E402
from backend.forecast import ForecastUnavailable, forecast_sulfur  # noqa: E402
from backend.formulas import LIMS_ALIASES  # noqa: E402
from backend.scenarios import ScenarioRequest  # noqa: E402
from tools.modeling.sulfur_forecast import HARD_LIMIT, _json_default, probability_metrics, regression_metrics  # noqa: E402

TARGET_METRIC = "lims.ht.2.Mg.Sulfur"
LIMS_DELAY = pd.Timedelta(minutes=240)
VAK_TARGETS = {
    "24-2000:GODT:T90": "90%.T", "24-2000:GODT:T50": "50%.T", "24-2000:GODT:I250": "I250", "24-2000:GODT:D15": "D15",
    "24-2000:GODT:CloudPoint": "CloudPoint", "24-2000:GODT:CFPP": "CFPP", "24-2000:GODT:T95": "95%.T", "24-2000:GODT:IBP": "IBP.T",
    "AVT6:240-350:D15": "D15", "AVT6:240-350:T50": "50%.T", "AVT6:240-350:CFPP": "CFPP", "AVT6:240-350:EBP": "EBP.T",
    "AVT6:350:CFPP": "CFPP", "AVT6:350:T50": "50%.T", "AVT6:350:I350": "I350", "AVT6:350:D15": "D15",
}


def load_labs(directory: Path, metric: str, start: str | None, end: str | None) -> pd.DataFrame:
    path = str(directory / "observations.parquet").replace("'", "''")
    with duckdb.connect(":memory:") as db:
        labs = db.execute(
            f"""SELECT timestamp, value FROM read_parquet('{path}') WHERE metric_id=? AND value IS NOT NULL AND isfinite(value)
                AND NOT contains(coalesce(flags,''), 'invalid') AND NOT contains(coalesce(flags,''), 'conflict') ORDER BY timestamp""",
            [metric]).fetchdf()
    labs["timestamp"] = pd.to_datetime(labs["timestamp"])
    if start:
        labs = labs[labs["timestamp"] >= pd.Timestamp(start)]
    if end:
        labs = labs[labs["timestamp"] < pd.Timestamp(end)]
    return labs.reset_index(drop=True)


def run_decisions(directory: Path, labs: pd.DataFrame, horizon: int) -> pd.DataFrame:
    rows = []
    started = time.time()
    for index, lab in labs.iterrows():
        origin = lab["timestamp"] - pd.Timedelta(minutes=horizon)
        request = ScenarioRequest(at=origin.isoformat(), horizon_minutes=horizon)
        decision = make_decision(directory, request)
        forecast = decision.get("forecast") or {}
        candidates = {c["id"]: c for c in decision.get("candidates", [])}
        hold = candidates.get("hold") or {}
        selected = candidates.get(decision.get("selected_candidate")) if decision.get("selected_candidate") else None
        try:
            nowcast_at_sample = forecast_sulfur(directory, lab["timestamp"].isoformat(), horizon_minutes=0)
        except ForecastUnavailable:
            nowcast_at_sample = {}
        quality = decision["agents"]["quality"]["evidence"]
        rows.append({
            "sample_time": lab["timestamp"], "origin": origin, "actual": float(lab["value"]),
            "decision_status": decision["status"], "abstain_reason": (decision.get("abstain") or {}).get("reason"),
            "selected_candidate": decision.get("selected_candidate"),
            "forecast_status": forecast.get("status"), "forecast_reasons": "|".join(forecast.get("reasons") or []),
            "forecast": forecast.get("prediction"), "forecast_lower": forecast.get("prediction_lower"),
            "forecast_upper": forecast.get("prediction_upper"), "exceedance_probability": forecast.get("exceedance_probability"),
            "alarm": forecast.get("alarm_above_10"), "alarm_probability": forecast.get("alarm_probability"),
            "nowcast_at_origin": (forecast.get("nowcast") or {}).get("prediction"),
            "nowcast_at_sample": (nowcast_at_sample.get("nowcast") or {}).get("prediction") if nowcast_at_sample.get("status") == "ok" else None,
            "baseline_source": quality["sulfur"]["source"], "baseline_sulfur": quality["sulfur"]["value"],
            "previous_lab": forecast.get("prediction_previous_lab"),
            "hold_predicted": hold.get("predicted_sulfur"), "hold_feasible": hold.get("feasible"),
            "selected_predicted": selected.get("predicted_sulfur") if selected else None,
            "selected_exceedance": selected.get("exceedance_probability") if selected else None,
            "selected_dT6": (selected.get("controls") or {}).get("ht.T6", {}).get("change") if selected else None,
            "gate_reasons": "|".join(dict.fromkeys(r for c in decision.get("candidates", []) for r in (c.get("safety_gate") or {}).get("reasons", []))),
        })
        if (index + 1) % 25 == 0:
            print(f"  {index + 1}/{len(labs)} decisions, {time.time() - started:.0f}s", file=sys.stderr)
    return pd.DataFrame(rows)


def summarize_decisions(frame: pd.DataFrame, horizon: int) -> dict:
    actual = frame["actual"].to_numpy(dtype=float)
    ok = frame["forecast_status"].eq("ok").to_numpy()
    accepted = frame[ok]
    exceed = frame["actual"] > HARD_LIMIT
    summary = {
        "horizon_minutes": horizon, "samples": int(len(frame)), "forecast_ok": int(ok.sum()), "forecast_coverage": float(ok.mean()),
        "actual_above_10": int(exceed.sum()), "actual_above_10_among_abstained": int((exceed & ~ok).sum()),
        "decision_status": frame["decision_status"].value_counts().to_dict(),
        "baseline_source": frame["baseline_source"].value_counts(dropna=False).astype(str).to_dict() if len(frame) else {},
        "selected_candidate": frame["selected_candidate"].value_counts(dropna=False).astype(str).to_dict() if len(frame) else {},
        "forecast_abstain_reasons": pd.Series([r for rs in frame.loc[~ok, "forecast_reasons"] for r in str(rs).split("|") if r and r != "nan"]).value_counts().to_dict(),
        "decision_abstain_reasons": frame.loc[frame["decision_status"].eq("abstain"), "abstain_reason"].fillna("").str.slice(0, 120).value_counts().head(12).to_dict(),
        "accepted": {
            "forecast_vs_lab": regression_metrics(accepted["actual"], accepted["forecast"]),
            "nowcast_at_origin_vs_lab": regression_metrics(accepted["actual"], accepted["nowcast_at_origin"]),
            "hold_candidate_vs_lab": regression_metrics(accepted["actual"], accepted["hold_predicted"]),
            "previous_lab_vs_lab": regression_metrics(accepted["actual"], accepted["previous_lab"]),
            "interval_80_coverage": float(((accepted["actual"] >= accepted["forecast_lower"]) & (accepted["actual"] <= accepted["forecast_upper"])).mean()) if len(accepted) else None,
            "exceedance_probability": probability_metrics(accepted["actual"], accepted["exceedance_probability"],
                                                          float(accepted["alarm_probability"].iloc[0]) if len(accepted) else 0.3),
        },
        "nowcast_at_sample_vs_lab": regression_metrics(frame["actual"], frame["nowcast_at_sample"]),
    }
    alarm = accepted["alarm"].fillna(False).astype(bool)
    dangerous = accepted["actual"] > HARD_LIMIT
    summary["alarm_before_real_exceedance"] = {
        "exceedances_with_forecast": int(dangerous.sum()), "alarms": int(alarm.sum()),
        "recall": float((alarm & dangerous).sum() / dangerous.sum()) if dangerous.any() else None,
        "false_alarm_rate": float((alarm & ~dangerous).sum() / (~dangerous).sum()) if (~dangerous).any() else None,
    }
    recommended = frame[frame["decision_status"].eq("recommendation")]
    summary["recommendations"] = {
        "count": int(len(recommended)),
        "with_temperature_increase": int((recommended["selected_dT6"].fillna(0) > 0.05).sum()),
        "actual_above_10_after_hold_recommendation": int(((recommended["selected_candidate"] == "hold") & (recommended["actual"] > HARD_LIMIT)).sum()),
        "actual_above_10_when_hold_was_infeasible": int(((frame["hold_feasible"] == False) & (frame["actual"] > HARD_LIMIT)).sum()),  # noqa: E712
        "hold_infeasible": int((frame["hold_feasible"] == False).sum()),  # noqa: E712
    }
    return summary


def _kip_frame(directory: Path, plant: str, tags: list[str]) -> pd.DataFrame:
    path = str(directory / "observations.parquet").replace("'", "''")
    ids = [f"{plant}.{tag}" for tag in tags]
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"""SELECT metric_id, timestamp, value FROM read_parquet('{path}') WHERE metric_id IN (SELECT unnest(?))
                AND value IS NOT NULL AND isfinite(value) AND NOT contains(coalesce(flags,''), 'invalid')
                AND NOT contains(coalesce(flags,''), 'conflict') ORDER BY timestamp""", [ids]).fetchdf()
    rows["timestamp"] = pd.to_datetime(rows["timestamp"])
    frame = rows.pivot_table(index="timestamp", columns="metric_id", values="value", aggfunc="last")
    frame.columns = [c.split(".", 1)[1] for c in frame.columns]
    return frame.sort_index()


def run_vak(directory: Path, start: str | None, end: str | None) -> tuple[pd.DataFrame, list[dict]]:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    path = str(directory / "observations.parquet").replace("'", "''")
    with duckdb.connect(":memory:") as db:
        lims_ids = [r[0] for r in db.execute(f"SELECT DISTINCT metric_id FROM read_parquet('{path}') WHERE source='lims'").fetchall()]
    rows, evidence = [], []
    for formula in registry["formulas"]:
        fid = formula["id"]
        parameter = VAK_TARGETS.get(fid)
        if parameter is None:
            continue
        expression = formula["expression"].replace(",", ".").replace("×", "*").replace("x", "*")
        lab_inputs = []
        for alias, (variable, metric_id) in LIMS_ALIASES.items():
            if alias in expression:
                expression = expression.replace(alias, variable)
                if (variable, metric_id) not in lab_inputs:
                    lab_inputs.append((variable, metric_id))
        plant = formula.get("plant", "ht" if fid.startswith("24") else "avt")
        tags = list(dict.fromkeys(re.findall(r"\b[A-Z]\d+\b", expression)))
        kip = _kip_frame(directory, plant, tags)
        missing = [t for t in tags if t not in kip.columns]
        if missing:
            evidence.append({"formula": fid, "status": "missing_inputs", "missing": missing})
            continue
        candidates = [m for m in lims_ids if m.startswith(f"lims.{plant}.") and m.endswith("." + parameter)]
        for metric in candidates:
            labs = load_labs(directory, metric, start, end)
            if len(labs) < 10:
                continue
            inputs = pd.merge_asof(labs.sort_values("timestamp"), kip.reset_index(), on="timestamp", direction="backward",
                                   tolerance=pd.Timedelta(minutes=30))
            for variable, lab_metric in lab_inputs:
                lab_series = load_labs(directory, lab_metric, None, None)
                lab_series["available_at"] = lab_series["timestamp"] + LIMS_DELAY
                lab_series = lab_series.rename(columns={"value": variable}).sort_values("available_at")
                inputs = pd.merge_asof(inputs.sort_values("timestamp"), lab_series[["available_at", variable]],
                                       left_on="timestamp", right_on="available_at", direction="backward",
                                       tolerance=pd.Timedelta(days=7)).drop(columns=["available_at"])
            try:
                computed = pd.eval(expression, local_dict={c: inputs[c] for c in inputs.columns if c != "timestamp"}, engine="python")
            except Exception as exc:  # noqa: BLE001
                evidence.append({"formula": fid, "lims": metric, "status": "evaluation_error", "error": str(exc)})
                continue
            computed = pd.Series(np.asarray(computed, dtype=float), index=inputs.index)
            computed[~np.isfinite(computed)] = np.nan
            valid = computed.notna() & inputs["value"].notna()
            n = int(valid.sum())
            if n < 10:
                continue
            error = computed[valid] - inputs.loc[valid, "value"]
            median_baseline = float(inputs.loc[valid, "value"].median())
            for ts, actual, pred in zip(inputs.loc[valid, "timestamp"], inputs.loc[valid, "value"], computed[valid]):
                rows.append({"formula": fid, "lims": metric, "timestamp": ts, "actual": actual, "computed": pred})
            evidence.append({
                "formula": fid, "lims": metric, "status": "ok", "n": n, "period": [str(inputs.loc[valid, "timestamp"].min()), str(inputs.loc[valid, "timestamp"].max())],
                "mae": float(error.abs().mean()), "rmse": float(np.sqrt((error ** 2).mean())), "bias_computed_minus_lab": float(error.mean()),
                "median_ae": float(error.abs().median()),
                "correlation": float(np.corrcoef(computed[valid], inputs.loc[valid, "value"])[0, 1]) if computed[valid].std() > 0 else None,
                "lab_std": float(inputs.loc[valid, "value"].std()),
                "mae_constant_median": float((inputs.loc[valid, "value"] - median_baseline).abs().mean()),
                "mae_after_bias_removal": float((error - error.median()).abs().mean()),
            })
    return pd.DataFrame(rows), evidence


def write_report(path: Path, decisions: dict, vak: list[dict], args: argparse.Namespace) -> None:
    acc = decisions["accepted"]
    rows = [
        "# Бэктест контура агентов и ВАК",
        "",
        f"Набор: `{args.dataset}`; период проб: {args.start or 'начало'} — {args.end or 'конец'}; горизонт решения {decisions['horizon_minutes']} мин. "
        "Контур запускался в момент `origin = время пробы − горизонт` только по данным, известным к origin; результат ЛИМС сравнивается постфактум.",
        "",
        "## Контур решений по пробам серы (гидроочистка, точка 2)",
        "",
        f"- Проб: {decisions['samples']}; прогноз выдан для {decisions['forecast_ok']} ({decisions['forecast_coverage']:.1%}); "
        f"фактических превышений 10 мг/кг: {decisions['actual_above_10']}, из них при отказе прогноза: {decisions['actual_above_10_among_abstained']}.",
        f"- Статусы решений: {decisions['decision_status']}; выбранные кандидаты: {decisions['selected_candidate']}.",
        f"- Источник базовой серы: {decisions['baseline_source']}.",
        f"- Причины отказа прогноза: {decisions['forecast_abstain_reasons']}.",
        "",
        "| оценка (принятые строки) | n | MAE | RMSE | bias | corr | recall >10 | ложные тревоги |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (("прогноз контура на горизонте", "forecast_vs_lab"), ("nowcast в момент origin", "nowcast_at_origin_vs_lab"),
                       ("кандидат hold (сценарная модель)", "hold_candidate_vs_lab"), ("предыдущая опубликованная проба", "previous_lab_vs_lab")):
        m = acc[key]
        if m.get("n"):
            rows.append(f"| {label} | {m['n']} | {m['mae']:.3f} | {m['rmse']:.3f} | {m['bias_pred_minus_actual']:+.3f} | "
                        f"{m['correlation'] if m['correlation'] is None else round(m['correlation'], 3)} | {m['recall_above_10']} | {m['false_alarm_rate']:.3f} |")
    now = decisions["nowcast_at_sample_vs_lab"]
    if now.get("n"):
        rows.append(f"| nowcast в момент отбора пробы (все строки) | {now['n']} | {now['mae']:.3f} | {now['rmse']:.3f} | {now['bias_pred_minus_actual']:+.3f} | "
                    f"{round(now['correlation'], 3) if now['correlation'] is not None else '—'} | {now['recall_above_10']} | {now['false_alarm_rate']:.3f} |")
    p = acc["exceedance_probability"]
    alarm = decisions["alarm_before_real_exceedance"]
    rec = decisions["recommendations"]
    rows += [
        "",
        f"- Вероятность превышения: Brier {p.get('brier')!s:.5} (климатология {p.get('brier_climatology')!s:.5}, skill {p.get('brier_skill')!s:.5}), AUC {p.get('auc')!s:.5}; "
        f"покрытие 80% интервала {acc['interval_80_coverage']!s:.4}.",
        f"- Тревога (P ≥ {p.get('alarm_probability')}) до реального превышения: recall {alarm['recall']!s:.4}, ложные тревоги {alarm['false_alarm_rate']!s:.4} "
        f"({alarm['alarms']} тревог, {alarm['exceedances_with_forecast']} превышений среди принятых).",
        f"- Рекомендаций: {rec['count']}, из них с повышением T6: {rec['with_temperature_increase']}; hold недопустим в {rec['hold_infeasible']} случаях, "
        f"из них реальное превышение наступило в {rec['actual_above_10_when_hold_was_infeasible']}; превышений после рекомендации hold: {rec['actual_above_10_after_hold_recommendation']}.",
        f"- Причины отказа контура (топ): {decisions['decision_abstain_reasons']}.",
        "",
        "Эффект рекомендованных изменений исторически не проверяем (контрфактических данных нет); проверяется уровень, тревоги и причины отказов.",
        "",
        "## ВАК против ЛИМС",
        "",
        "Для 24-2000 сопоставление с точкой 2 однозначно; для АВТ приведены все точки с тем же показателем, лучшая по MAE — гипотеза, не подтверждённое соответствие.",
        "",
        "| формула | ряд ЛИМС | n | MAE | bias | corr | MAE после снятия смещения | MAE константы (медиана ЛИМС) | σ ЛИМС |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in sorted(vak, key=lambda i: (i["formula"], i.get("mae", 1e9))):
        if item.get("status") != "ok":
            rows.append(f"| {item['formula']} | {item.get('lims', '—')} | — | {item['status']} | | | | | |")
            continue
        rows.append(f"| {item['formula']} | {item['lims']} | {item['n']} | {item['mae']:.2f} | {item['bias_computed_minus_lab']:+.2f} | "
                    f"{item['correlation'] if item['correlation'] is None else round(item['correlation'], 2)} | {item['mae_after_bias_removal']:.2f} | "
                    f"{item['mae_constant_median']:.2f} | {item['lab_std']:.2f} |")
    rows += [
        "",
        "Интерпретация: формула полезна как виртуальный анализатор, если её MAE после снятия постоянного смещения заметно ниже MAE константы и корреляция положительна. "
        "Большое смещение при хорошей корреляции означает, что формула отслеживает динамику, но требует калибровки по ЛИМС; отрицательная или нулевая корреляция — формула не описывает этот ряд.",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--horizon-minutes", type=int, default=180)
    parser.add_argument("--vak-start", default=None, help="Period for the VAK comparison (default: whole history)")
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N samples (smoke test)")
    parser.add_argument("--skip-decisions", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    directory = args.dataset.resolve()
    labs = load_labs(directory, TARGET_METRIC, args.start, args.end)
    if args.limit:
        labs = labs.head(args.limit)
    result = {"dataset": str(directory), "start": args.start, "end": args.end, "horizon_minutes": args.horizon_minutes}
    if not args.skip_decisions:
        print(f"decision backtest over {len(labs)} samples", file=sys.stderr)
        decisions = run_decisions(directory, labs, args.horizon_minutes)
        decisions.to_csv(args.output / "decisions.csv", index=False, encoding="utf-8-sig")
        result["decisions"] = summarize_decisions(decisions, args.horizon_minutes)
    else:
        result["decisions"] = json.loads((args.output / "results.json").read_text(encoding="utf-8"))["decisions"]
    print("VAK backtest", file=sys.stderr)
    vak_rows, vak = run_vak(directory, args.vak_start, args.end)
    vak_rows.to_csv(args.output / "vak.csv", index=False, encoding="utf-8-sig")
    result["vak"] = vak
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(args.output / "REPORT.md", result["decisions"], vak, args)
    print(json.dumps({"output": str(args.output), "samples": len(labs), "decision_status": result["decisions"]["decision_status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
