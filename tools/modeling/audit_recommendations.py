"""Audit of the recommendation contour: does it advise when an option exists, and only then?

The contour is run exactly as the API runs it at every moment of a regular
grid (only data known at that moment).  Each refusal is classified by its
root cause.  For every refusal that is not caused by the input data (the
reliability gate passed, so candidates were evaluated), an independent
exhaustive "oracle" searches a dense grid of T6/F9/P13 moves (4725 points,
denser than the contour's own search) inside the per-step model limit with the
same scenario model and the same safety gate.
If the oracle finds an admissible move, the contour missed a recommendation.
For every recommendation the oracle reports whether a cheaper admissible move
exists (optimality gap of the candidate generator).

The oracle checks the completeness of the search, not the physics: both use
the same transparent scenario model, whose response coefficients are
observational assumptions.

Example::

    python tools/modeling/audit_recommendations.py --dataset storage/hackathon \\
        --start 2026-01-01T03:00 --end 2026-08-06T23:00 --step 6h \\
        --output reports/verification/recommendation-audit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("OILCODE_DECISION_LOG", "0")

from backend.agents import (  # noqa: E402
    AgentContext,
    OptimizationAgent,
    make_decision,
    required_additive,
)
from backend.analytics import snapshot  # noqa: E402
from backend.objectives import candidate_objectives  # noqa: E402
from backend.scenarios import (  # noqa: E402
    HARD_SULFUR_MAX,
    STEP_LIMITS,
    ControlChanges,
    ScenarioRequest,
    calculate_scenario,
)

# Denser than the contour's own search (T6 step 1 °C, P13 step 0.1 MPa), so a gap
# also measures what the contour loses to its discretisation.
ORACLE_GRID = {
    "temperature": [v / 2 for v in range(-12, 13)],
    "feed_rate_pct": [float(v) for v in range(-10, 11)],
    "pressure": [round(v * 0.05, 2) for v in range(-4, 5)],
}
assert all(max(abs(v) for v in values) <= STEP_LIMITS[key] for key, values in ORACLE_GRID.items())

CAUSES = (
    ("останов", "shutdown_or_transition"), ("пуска", "shutdown_or_transition"),
    ("не свежее", "stale_or_missing_telemetry"), ("отсутствует конечное", "stale_or_missing_telemetry"),
    ("недостоверные данные", "flagged_telemetry"),
    ("Нет допустимого модельного прогноза", "no_forecast"),
    ("области применимости", "forecast_path_unsupported"),
    ("анализаторы серы расходятся", "analyser_conflict"),
)


def _cause(decision: dict) -> str:
    if decision["status"] == "recommendation":
        return "recommendation"
    reasons = decision["agents"]["reliability"]["reasons"]
    if reasons:
        for key, label in CAUSES:
            if key in reasons[0]:
                return label
        return "data_gate_other"
    text = " ".join(r for c in decision.get("candidates") or [] for r in (c.get("safety_gate") or {}).get("reasons", []))
    tags = [label for key, label in (("Риск превышения", "sulfur_risk"), ("не оставляет запаса", "sulfur_risk"),
                                     ("превышает обязательный предел", "sulfur_risk"), ("t95", "t95"),
                                     ("Цетановое", "cetane"), ("cetane", "cetane"), ("Индекс нагрузки", "severity"))
            if key in text]
    return "no_admissible_option:" + "+".join(sorted(set(tags)) or ["other"])


def oracle(directory: Path, request: ScenarioRequest, decision: dict) -> dict:
    """Best admissible move on the dense grid under the contour's own rules."""
    quality, reliability = decision["agents"]["quality"], decision["agents"]["reliability"]
    dose = required_additive(request, quality)
    if dose and dose["feasible"] and dose["pct"] > request.additive_pct:
        request = request.model_copy(update={"additive_pct": dose["pct"]}, deep=True)
    frame = snapshot(directory, request.at)
    forecast = quality["evidence"].get("model_forecast")
    agent = OptimizationAgent()
    best, admissible = None, 0
    for temperature in ORACLE_GRID["temperature"]:
        for feed in ORACLE_GRID["feed_rate_pct"]:
            for pressure in ORACLE_GRID["pressure"]:
                changes = ControlChanges(temperature=temperature, feed_rate_pct=feed, pressure=pressure)
                trial = request.model_copy(update={"changes": changes})
                try:
                    scenario = calculate_scenario(directory, trial, frame=frame, forecast=forecast)
                except ValueError:
                    continue
                # Cost-saving or mixed moves must end in the middle of the risk zone, as in the contour.
                gate = agent._gate(scenario, AgentContext(directory, trial, frame), reliability, quality,
                                   economic=not OptimizationAgent._is_corrective(changes))
                effort = agent._effort(changes)
                objectives = candidate_objectives(scenario, effort)
                severity = objectives["regime_severity"]
                if not gate["passed"] or severity.get("within_model_limit") is not True:
                    continue
                if scenario["product_sulfur"] > min(request.targets.sulfur_max, HARD_SULFUR_MAX):
                    continue
                admissible += 1
                key = (objectives["ranking_loss"], effort)
                if best is None or key < best[0]:
                    best = (key, changes, scenario["risk"].get("exceedance_probability"))
    return {"admissible_points": admissible, "grid_points": len(ORACLE_GRID["temperature"]) * len(ORACLE_GRID["feed_rate_pct"])
            * len(ORACLE_GRID["pressure"]),
            "best_loss": best[0][0] if best else None,
            "best_changes": best[1].model_dump() if best else None,
            "best_probability": best[2] if best else None}


def audit_one(args: tuple[str, str, dict]) -> dict:
    dataset, at, extra = args
    directory = Path(dataset)
    request = ScenarioRequest(at=at, **extra)
    try:
        decision = make_decision(directory, request)
    except Exception as exc:  # the audit must record, not hide, a crash
        return {"at": at, "status": "error", "cause": "error", "error": repr(exc)}
    forecast = decision.get("forecast") or {}
    quality = decision["agents"]["quality"]["evidence"]
    selected = next((c for c in decision["candidates"] if c["id"] == decision.get("selected_candidate")), None)
    recommendation = decision.get("recommendation") or {}
    row = {
        "at": at, "status": decision["status"], "cause": _cause(decision),
        "selected": decision.get("selected_candidate"),
        "forecast_status": forecast.get("status"), "forecast_h": forecast.get("prediction"),
        "hold_probability": next(((c.get("risk") or {}).get("exceedance_probability") for c in decision["candidates"]
                                  if c["id"] == "hold"), None),
        "alarm": forecast.get("alarm_above_10"),
        "baseline_sulfur": quality["sulfur"]["value"],
        "cetane_last": quality["other_quality"]["cetane"].get("value"),
        "cetane_lower_bound": quality["other_quality"]["cetane"].get("lower_bound"),
        "t95": quality["other_quality"]["t95"].get("value"),
        "selected_sulfur": selected.get("product_sulfur") if selected else None,
        "selected_probability": (recommendation.get("risk") or {}).get("exceedance_probability"),
        "selected_loss": (selected.get("objectives") or {}).get("ranking_loss") if selected else None,
        "dT6": (recommendation.get("controls") or {}).get("ht.T6", {}).get("change"),
        "dF9_pct": (recommendation.get("controls") or {}).get("ht.F9", {}).get("change"),
        "dP13": (recommendation.get("controls") or {}).get("ht.P13", {}).get("change"),
        "additive_pct": (recommendation.get("additive") or {}).get("pct"),
        "conflicts": ";".join(f"{c['code']}:{c['resolution']}" for c in decision.get("conflicts") or []),
        "reason": (decision.get("abstain") or {}).get("reason"),
        "consistency_failed": sum(not c["passed"] for c in decision.get("consistency") or []),
    }
    run_oracle = (decision["agents"]["reliability"].get("can_recommend") and not request.tanks
                  and (decision["status"] == "abstain" or row["selected_loss"] is not None))
    if run_oracle:
        found = oracle(directory, request, decision)
        row.update({f"oracle_{k}": v for k, v in found.items() if k != "best_changes"},
                   oracle_best_changes=json.dumps(found["best_changes"]) if found["best_changes"] else None)
    return row


def summarize(frame: pd.DataFrame, minimum_gain: float) -> dict:
    abstain = frame[frame.status == "abstain"]
    evaluated = abstain[abstain.cause.str.startswith("no_admissible_option")]
    missed = evaluated[evaluated.get("oracle_admissible_points", pd.Series(0, index=evaluated.index)).fillna(0) > 0]
    recommended = frame[frame.status == "recommendation"]
    gap = (recommended.selected_loss - recommended.oracle_best_loss) if "oracle_best_loss" in recommended else pd.Series(dtype=float)
    return {
        "moments": int(len(frame)),
        "status": {k: int(v) for k, v in frame.status.value_counts().items()},
        "causes": {k: int(v) for k, v in frame.cause.value_counts().items()},
        "selected": {str(k): int(v) for k, v in recommended.selected.value_counts().items()},
        "refusals_after_candidate_search": int(len(evaluated)),
        "missed_recommendations": int(len(missed)),
        "missed_examples": missed[["at", "cause", "oracle_best_changes", "oracle_best_probability"]].head(10).to_dict("records")
        if len(missed) else [],
        "recommendations_with_cheaper_admissible_move": int((gap > minimum_gain + 1e-9).sum()),
        "max_optimality_gap": float(gap.max()) if len(gap.dropna()) else None,
        "selected_probability_max": float(recommended.selected_probability.max()) if recommended.selected_probability.notna().any() else None,
        "selected_sulfur_max": float(recommended.selected_sulfur.max()) if len(recommended) else None,
        "corrective_under_alarm": int(((recommended.alarm == True) & (recommended.selected != "hold")).sum()),  # noqa: E712
        "with_additive": int((recommended.additive_pct.fillna(0) > 0).sum()),
        "consistency_failures": int(frame.consistency_failed.fillna(0).sum()),
        "errors": int((frame.status == "error").sum()),
    }


CAUSE_TEXT = {
    "recommendation": "рекомендация",
    "shutdown_or_transition": "останов / пуск (F9 < 60% среднего)",
    "no_forecast": "нет допустимого прогноза",
    "forecast_path_unsupported": "траектория прогноза вне области применимости",
    "stale_or_missing_telemetry": "устаревшая или отсутствующая телеметрия",
    "flagged_telemetry": "недостоверная телеметрия",
    "analyser_conflict": "расхождение анализаторов",
    "forecast_alarm_refusal": "тревога прогноза → немедленный отказ (старый контур)",
}


def _cause_text(cause: str) -> str:
    if cause.startswith("no_admissible_option:"):
        parts = {"sulfur_risk": "риск/запас по сере", "cetane": "цетан", "t95": "T95", "severity": "нагрузка",
                 "other": "прочее"}
        return "нет допустимого варианта: " + ", ".join(parts.get(p, p) for p in cause.split(":", 1)[1].split("+"))
    return CAUSE_TEXT.get(cause, cause)


def write_report(output: Path) -> Path:
    """REPORT.md from every ``*.json`` summary in ``output`` (baseline first, then runs)."""
    summaries = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(output.glob("*.json"))]
    summaries.sort(key=lambda s: (not s["label"].startswith("baseline"), s["label"] != "default", s["label"]))
    pct = lambda n, total: f"{n} ({100 * n / total:.1f}%)" if total else str(n)  # noqa: E731
    lines = ["# Аудит рекомендаций контура агентов", "",
             "Контур запускается так же, как API, на каждом моменте сетки (только данные, известные к моменту).",
             "Для каждого отказа после перебора вариантов независимый оракул перебирает 4725 ходов T6/F9/P13 в пределах",
             "модельного шага (сетка плотнее контурной) той же сценарной моделью и той же проверкой. Найденный допустимый",
             "ход = пропущенная рекомендация. Для каждой рекомендации оракул ищет более дешёвый допустимый ход.",
             "Оракул проверяет полноту поиска, а не физику: коэффициенты отклика — наблюдательные допущения.", "",
             "Воспроизведение: `python tools/modeling/audit_recommendations.py --start 2026-01-01T03:00 "
             "--end 2026-08-06T23:00 --step 6h --output reports/verification/recommendation-audit`, затем `--report`.", "",
             "## Сводка", "",
             "| прогон | запрос | моментов | рекомендаций | пропущено оракулом | дешевле на > порога | max P(S>10) выбранного | max сера выбранного | с присадкой | коррекция при тревоге | ошибки согласованности |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in summaries:
        total = s["moments"]
        recs = s["status"].get("recommendation", 0)
        oracle = "—" if s["label"].startswith("baseline") else str(s.get("missed_recommendations"))
        cheaper = "—" if s["label"].startswith("baseline") else str(s.get("recommendations_with_cheaper_admissible_move"))
        probability = s.get("selected_probability_max")
        lines.append(f"| {s['label']} | `{json.dumps(s.get('request') or {}, ensure_ascii=False)}` | {total} | {pct(recs, total)} | "
                     f"{oracle} | {cheaper} | {'—' if probability is None else f'{probability:.3f}'} | "
                     f"{s.get('selected_sulfur_max') or 0:.2f} | {s.get('with_additive', '—')} | "
                     f"{s.get('corrective_under_alarm', '—')} | {s.get('consistency_failures', '—')} |")
    for s in summaries:
        lines += ["", f"## {s['label']}", ""]
        if s.get("note"):
            lines += [s["note"], ""]
        if s["label"].startswith("baseline"):
            lines += [f"Выбранные варианты: {s['selected']}. У {s['selected_probability_over_half_budget']} из "
                      f"{s['status'].get('recommendation', 0)} рекомендаций P(S>10) > 0,1, у {s['selected_probability_over_budget']} — "
                      "выше порога тревоги 0,2.", ""]
        else:
            lines += [f"Выбранные варианты: {s['selected']}. Отказов после перебора вариантов: "
                      f"{s['refusals_after_candidate_search']}, из них оракул нашёл допустимый ход: {s['missed_recommendations']}. "
                      f"Максимальный разрыв потерь с оракулом: {s.get('max_optimality_gap') or 0:.3f} "
                      f"(удержание предпочитается при выигрыше < 0,03).", ""]
        lines += ["| причина | моментов |", "|---|---:|"]
        lines += [f"| {_cause_text(cause)} | {count} |" for cause, count in s["causes"].items()]
    lines += ["", "## Ограничения", "",
              "- 2026 год уже использовался в исследованиях модели и не является слепым тестом.",
              "- Эффект рекомендованных изменений исторически не проверяем: контрфактических данных нет. Проверяются полнота поиска,",
              "  соблюдение бюджета риска и причины отказов.",
              "- Прогноз на 3 ч лишь на 2% точнее константы (MAE 1.452 против 1.481); запас по сере опирается на калиброванные",
              "  остатки модели, а не на точность точечного прогноза.",
              "- Коэффициенты отклика и предел шага — модельные допущения; снижение серы засчитывается по меньшей, рост — по большей",
              "  из двух оценок."]
    path = output / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="storage/hackathon")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--step", default="6h")
    parser.add_argument("--request", default="{}", help="JSON overrides of ScenarioRequest for every moment")
    parser.add_argument("--label", default="default")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--report", action="store_true", help="only rebuild REPORT.md from the summaries in --output")
    args = parser.parse_args()
    if args.report:
        print(write_report(args.output))
        return
    if not args.start or not args.end:
        parser.error("--start and --end are required unless --report is given")

    extra = json.loads(args.request)
    moments = [t.isoformat() for t in pd.date_range(args.start, args.end, freq=args.step)]
    started = time.time()
    with Pool(args.processes) as pool:
        rows = pool.map(audit_one, [(args.dataset, at, extra) for at in moments], chunksize=2)
    frame = pd.DataFrame(rows)
    minimum_gain = ScenarioRequest(at=moments[0], **extra).minimum_economic_gain
    summary = {"label": args.label, "request": extra, "dataset": args.dataset, "start": args.start, "end": args.end,
               "step": args.step, "oracle_grid": ORACLE_GRID, "seconds": round(time.time() - started, 1),
               **summarize(frame, minimum_gain)}
    args.output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output / f"{args.label}.csv", index=False)
    (args.output / f"{args.label}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("label", "moments", "status", "causes", "missed_recommendations",
                                               "recommendations_with_cheaper_admissible_move", "seconds")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
