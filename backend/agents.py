"""Deterministic multi-agent decision support for the local prototype.

Four roles with an explicit information exchange (``trace[i].consumes`` /
``produces``):

* ``QualityAgent`` — current and forecast product quality, the detected
  problem (does the situation require an action?) and the data behind it.
* ``ReliabilityAgent`` — freshness/availability gates, the regime-risk index
  with its factors, the *constraints* the optimiser must respect and a
  documented data-quality confidence score.
* ``OptimizationAgent`` — a deterministic grid of admissible control moves
  evaluated with the transparent scenario model, hard gates, multi-criteria
  objectives and a Pareto front, then a weighted selection.
* ``Orchestrator`` — conflict resolution between the roles, consistency
  checks across their outputs, the final recommendation (or a reasoned
  abstention), alternatives and an operator explanation.

Every agent is a small transparent Python rule set; the decision is
reproducible offline in the closed hackathon network.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from .analytics import parse_time, snapshot
from .candidates import candidate_grid
from .explain import build_explanation
from .forecast import ForecastUnavailable, applicable_model, forecast_sulfur
from .objectives import candidate_objectives, pareto_front, risk_assessment
from .scenarios import (
    FEED_RATE_CHANGE_LIMIT_PCT,
    HARD_CETANE_MIN,
    HARD_SULFUR_MAX,
    HARD_T95_MAX,
    PRESSURE_CHANGE_LIMIT,
    TEMPERATURE_CHANGE_LIMIT,
    ControlChanges,
    ScenarioRequest,
    calculate_scenario,
    select_baseline,
    select_sulfur,
)

CONTROL_IDS = ("ht.T6", "ht.F9", "ht.P13")
UNUSABLE_FLAGS = {"invalid", "conflict", "suspect", "flatline", "gap"}
# Slowly varying product properties are sampled rarely (cetane: ~monthly in
# the source LIMS; T95 daily but its day-to-day persistence beats the VAK
# soft sensor, see reports/verification/agent-backtest).  Their last
# laboratory value stays usable as a baseline constraint for a declared
# window instead of the 48-hour sulphur freshness.  A prototype assumption:
# the hydro-treating scenario does not model a T95/cetane response, so the
# window only affects whether the baseline check can be performed at all.
SLOW_QUALITY_MAX_AGE_MINUTES = {"lims.ht.2.CetaneNumber": 60 * 24 * 60, "lims.ht.2.95%.T": 7 * 24 * 60}
STEP_LIMITS = {"ht.T6": TEMPERATURE_CHANGE_LIMIT, "ht.F9": FEED_RATE_CHANGE_LIMIT_PCT, "ht.P13": PRESSURE_CHANGE_LIMIT}
# Reliability constraints by risk class: multiplier on upward T6/P13 steps.
RISK_STEP_FACTORS = {"normal": 1.0, "elevated": 0.5, "high": 0.0}
CONFIDENCE_CLASSES = ((0.75, "high"), (0.5, "medium"))


@dataclass(frozen=True)
class AgentContext:
    directory: Path
    request: ScenarioRequest
    frame: dict[str, Any]
    model_entry: dict[str, Any] | None = None


def _values(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["metric_id"]: item for item in frame.get("values", [])}


def _evidence(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {"value": None, "timestamp": None, "freshness": "missing", "flags": []}
    return {
        "value": item.get("value"),
        "timestamp": item.get("timestamp"),
        "freshness": item.get("freshness"),
        "age_minutes": item.get("age_minutes"),
        "flags": item.get("flags", []),
        "available_at": item.get("available_at"),
    }


def _evidence_failures(label: str, item: dict[str, Any], at: str, max_age_minutes: float | None = None) -> list[str]:
    """Hard gates; a confidence score cannot compensate for a bad signal.

    ``max_age_minutes`` widens the freshness window for declared slow
    properties; the age must still be known and the value published.
    """
    reasons = []
    value = item.get("value")
    if value is None or not isfinite(value):
        reasons.append(f"{label}: отсутствует конечное численное значение")
    if item.get("freshness") != "fresh":
        age = item.get("age_minutes")
        within_window = max_age_minutes is not None and age is not None and age <= max_age_minutes
        if not within_window:
            reasons.append(f"{label}: измерение не свежее ({item.get('freshness') or 'unknown'})")
    bad_flags = UNUSABLE_FLAGS.intersection(item.get("flags") or [])
    if bad_flags:
        reasons.append(f"{label}: недостоверные данные ({', '.join(sorted(bad_flags))})")
    timestamp = item.get("timestamp")
    available_at = item.get("available_at") or timestamp
    if not timestamp or not available_at:
        reasons.append(f"{label}: неизвестно время измерения/доступности")
    else:
        try:
            if parse_time(timestamp) > parse_time(at) or parse_time(available_at) > parse_time(at):
                reasons.append(f"{label}: измерение ещё не доступно на момент решения")
        except (TypeError, ValueError):
            reasons.append(f"{label}: некорректное время измерения/доступности")
    return reasons


class QualityAgent:
    """Estimates current and forecast product quality, detects the problem and exposes the data behind it."""

    role = "quality"

    def run(self, context: AgentContext) -> dict[str, Any]:
        values = _values(context.frame)
        request = context.request
        warnings = []
        model_forecast = None
        try:
            model_forecast = forecast_sulfur(context.directory, request.at, horizon_minutes=request.horizon_minutes)
        except ForecastUnavailable as exc:
            warnings.append(f"Прогноз серы недоступен: {exc}")
        forecast_ok = bool(model_forecast and model_forecast.get("status") == "ok" and model_forecast.get("nowcast"))

        raw_sulfur, raw_source = select_sulfur(values)
        raw_item = values.get(raw_source) if raw_source else None
        sulfur, sulfur_source, sulfur_item = select_baseline(request, values, model_forecast)

        controls = {metric_id: _evidence(values.get(metric_id)) for metric_id in CONTROL_IDS}
        available_controls = [metric_id for metric_id, evidence in controls.items() if evidence["value"] is not None]
        missing = [] if sulfur is not None else ["sulfur_baseline"]
        status = "ok" if not missing else "insufficient"
        if sulfur_item and sulfur_item.get("freshness") == "stale":
            warnings.append("Базовое измерение серы устарело")
        if not available_controls:
            warnings.append("Нет текущих значений управляющих тегов T6/F9/P13")
        if request.current_sulfur is not None:
            warnings.append("Базовая сера введена вручную: это условный сценарий, а не подтверждение качества продукта")
        if model_forecast and model_forecast.get("status") == "abstain":
            warnings.extend(model_forecast.get("reasons", []))
        if model_forecast and model_forecast.get("alarm_above_10"):
            warnings.append(
                f"Прогноз без воздействия: вероятность превышения 10 мг/кг {model_forecast['exceedance_probability']:.0%} "
                f"(порог тревоги {model_forecast['alarm_probability']:.0%}); требуется корректирующее действие или ручная проверка"
            )
        if forecast_ok and (model_forecast.get("stage1") or {}).get("status") == "fallback_persistence":
            warnings.append("Динамика анализатора недоступна: путь без воздействия — персистентность")

        # Problem detection: the trigger for a corrective candidate.
        # The decision concerns the horizon endpoint: the no-action path of
        # the forecast when it is usable, otherwise the (flat) current level.
        # The forecast alarm (F1-optimal threshold of the model) is a warning;
        # whether an action is *required* follows the decision targets.
        problem_reasons = []
        target = request.targets.sulfur_max
        if forecast_ok and request.current_sulfur is None and model_forecast.get("prediction") is not None:
            if model_forecast["prediction"] > target:
                problem_reasons.append(f"Прогноз без воздействия на горизонте {model_forecast['prediction']:.1f} мг/кг выше цели {target:g} мг/кг")
        elif sulfur is not None and sulfur > target:
            problem_reasons.append(f"Текущая оценка серы {sulfur:.1f} мг/кг выше цели {target:g} мг/кг (путь без воздействия — плоский)")
        if forecast_ok and model_forecast.get("exceedance_probability") is not None \
                and model_forecast["exceedance_probability"] > request.targets.max_exceedance_probability:
            problem_reasons.append(f"P(>10) без воздействия {model_forecast['exceedance_probability']:.0%} выше допустимой "
                                   f"{request.targets.max_exceedance_probability:.0%}")

        other_quality = {}
        for name, supplied, metric_id in (
            ("t95", request.current_t95, "lims.ht.2.95%.T"),
            ("cetane", request.current_cetane, "lims.ht.2.CetaneNumber"),
        ):
            other_quality[name] = (
                {"source": f"request.current_{name}", "value": supplied}
                if supplied is not None
                else {"source": metric_id, **_evidence(values.get(metric_id)), "max_age_minutes": SLOW_QUALITY_MAX_AGE_MINUTES[metric_id]}
            )
        previous_lab = (model_forecast or {}).get("previous_lab") if model_forecast else None
        lab_age_minutes = None
        if previous_lab and previous_lab.get("sample_time"):
            lab_age_minutes = (parse_time(request.at) - parse_time(previous_lab["sample_time"])).total_seconds() / 60
        elif raw_item and raw_source == "lims.ht.2.Mg.Sulfur":
            lab_age_minutes = raw_item.get("age_minutes")

        return {
            "role": self.role,
            "status": status,
            "summary": (
                ("Базовое качество доступно; " + ("требуется действие: " + "; ".join(problem_reasons) if problem_reasons
                                                else "проблем с качеством не обнаружено"))
                if status == "ok"
                else "Невозможно определить базовую серу"
            ),
            "evidence": {
                # Put the explicit request value after evidence so a missing
                # telemetry item cannot overwrite it with ``None``.
                "sulfur": {"source": sulfur_source, **_evidence(sulfur_item), "value": sulfur},
                "last_lab_sulfur": {"source": raw_source, **_evidence(raw_item)},
                "controls": controls,
                "available_control_count": len(available_controls),
                "model_forecast": model_forecast,
                "other_quality": other_quality,
                "in_flight_controls": (model_forecast or {}).get("in_flight_controls") if forecast_ok else None,
                "data_age": {"last_lab_minutes": lab_age_minutes,
                             "analysers": {name: item.get("age_minutes") for name, item in ((model_forecast or {}).get("analysers") or {}).items()}},
            },
            "problem": {"requires_action": bool(problem_reasons), "reasons": problem_reasons},
            "warnings": warnings,
            "missing": missing,
        }


class ReliabilityAgent:
    """Freshness gates, regime risk with factors, constraints for the optimiser and a data-quality confidence."""

    role = "reliability"

    @staticmethod
    def _constraints(context: AgentContext, controls: dict[str, dict], risk: dict) -> dict[str, dict]:
        entry = context.model_entry
        support = None
        if entry:
            horizon = max(entry["support"], key=int)
            support = entry["support"][horizon]
        factor = RISK_STEP_FACTORS.get(risk.get("class") or "normal", 1.0)
        out = {}
        for metric_id in CONTROL_IDS:
            tag = metric_id.split(".")[1]
            limit = STEP_LIMITS[metric_id]
            rule = {"min": None, "max": None, "max_step_up": limit, "max_step_down": limit, "blocked": False, "reason": None,
                    "relative": metric_id == "ht.F9", "basis": "prototype step limit"}
            if support is not None:
                i = list(support["feature_columns"]).index(tag)
                rule["min"], rule["max"] = float(support["support_lower"][i]), float(support["support_upper"][i])
                rule["basis"] = "train support of the applicable forecast model (0.5–99.5% + margin) ∩ prototype step limit"
            evidence = controls.get(metric_id) or {}
            failures = _evidence_failures(metric_id, evidence, context.request.at)
            if failures:
                rule["blocked"], rule["reason"] = True, "; ".join(failures)
            elif metric_id in ("ht.T6", "ht.P13") and factor < 1.0:
                rule["max_step_up"] = limit * factor
                rule["reason"] = (f"риск режима {risk.get('class')}: шаг вверх ограничен {rule['max_step_up']:g}"
                                  if factor > 0 else f"риск режима {risk.get('class')}: повышение заблокировано")
            out[metric_id] = rule
        return out

    @staticmethod
    def _confidence(quality: dict[str, Any], explicit: bool) -> dict[str, Any]:
        evidence = quality["evidence"]
        forecast = evidence.get("model_forecast") or {}
        factors = []

        def factor(name, score, note, value=None):
            factors.append({"name": name, "score": round(float(score), 3), "note": note, "value": value})

        if explicit:
            factor("hypothetical_baseline", 0.9, "база задана вручную: условный сценарий, качество данных прогноза не оценивается")
        age = (evidence.get("data_age") or {}).get("last_lab_minutes")
        if explicit:
            pass
        elif age is None:
            factor("last_lab_age", 0.4, "последняя опубликованная проба серы неизвестна")
        else:
            factor("last_lab_age", 1.0 if age <= 30 * 60 else 0.7 if age <= 48 * 60 else 0.4,
                   "возраст последней опубликованной пробы серы", round(age / 60, 1))
        pairs = (forecast.get("lab_anchor") or {}).get("pairs")
        if pairs is not None:
            factor("lab_anchor_pairs", 1.0 if pairs >= 8 else 0.8 if pairs >= 3 else 0.5, "число пар ЛИМС/анализатор для калибровки", pairs)
        analysers = forecast.get("analysers") or {}
        fresh = sum(1 for a in analysers.values() if a.get("value") is not None and a.get("age_minutes") is not None and a["age_minutes"] <= 30)
        if analysers:
            factor("analysers_fresh", {2: 1.0, 1: 0.8}.get(fresh, 0.5), "свежие поточные анализаторы серы (Q21, ПАК)", fresh)
        if forecast.get("feature_count"):
            missing = forecast.get("imputed_feature_count", 0) / forecast["feature_count"]
            factor("imputed_features", max(0.0, 1.0 - missing), "доля признаков прогноза, заменённых медианами", round(missing, 2))
        stage1 = (forecast.get("stage1") or {}).get("status")
        if not explicit:
            factor("forecast_status", 1.0 if forecast.get("status") == "ok" else 0.5, "статус модельного прогноза", forecast.get("status"))
            factor("stage1", {"ok": 1.0, "fallback_persistence": 0.8}.get(stage1, 0.9), "динамика анализатора (ступень 1)", stage1)
        score = 1.0
        for f in factors:
            score *= f["score"]
        klass = next((label for edge, label in CONFIDENCE_CLASSES if score >= edge), "low")
        return {"score": round(score, 3), "class": klass, "factors": factors,
                "meaning": "эвристический индекс качества входных данных прогноза [0, 1] (произведение факторов); риск режима учитывается "
                           "отдельно через ограничения; не вероятность и не гарантия"}

    def run(self, context: AgentContext, quality: dict[str, Any]) -> dict[str, Any]:
        sulfur = quality["evidence"]["sulfur"]
        explicit = sulfur["source"] == "request.current_sulfur"
        missing = list(quality.get("missing", []))
        warnings = list(quality.get("warnings", []))
        reasons = ["Недостаточно данных: отсутствует базовое значение серы"] if missing else []
        # A nowcast baseline is governed by the forecast applicability gate
        # (fresh analysers, lab anchor, regime support); raw sources use the
        # snapshot freshness/flag gates.
        if not explicit and sulfur["source"] != "model.nowcast":
            reasons.extend(_evidence_failures("Базовая сера", sulfur, context.request.at))
        # All three controls define the intervention vector.  Missing, stale or
        # flagged inputs block it even when another input has high confidence.
        controls = quality["evidence"]["controls"]
        for metric_id, evidence in controls.items():
            reasons.extend(_evidence_failures(metric_id, evidence, context.request.at))
        forecast = quality["evidence"].get("model_forecast")
        if not explicit and (not forecast or forecast.get("status") != "ok"):
            reasons.append("Нет допустимого модельного прогноза для решения по наблюдаемым данным")
        # A forecast alarm does not block the contour: it is the trigger for a
        # corrective candidate, which the safety gate then checks per candidate.
        can_recommend = not reasons
        values = _values(context.frame)
        risk = risk_assessment({key: item.get("value") for key, item in controls.items()},
                               {mid: (values.get(mid) or {}).get("value") for mid in values}, context.model_entry)
        if risk.get("status") == "ok" and risk.get("class") == "high":
            warnings.append(f"Высокий риск режима ({risk.get('dominant')}): повышение T6/P13 заблокировано агентом надёжности")
        constraints = self._constraints(context, controls, risk)
        confidence = self._confidence(quality, explicit)
        reason = "; ".join(reasons) if reasons else None
        return {
            "role": self.role,
            "status": "ok" if can_recommend else "insufficient",
            "summary": (
                f"Данные пригодны; риск режима {risk.get('class') or 'не оценён'}; уверенность {confidence['class']} ({confidence['score']:.2f})"
                if can_recommend
                else reason
            ),
            "confidence": confidence,
            "risk": risk,
            "regime_severity": risk.get("severity"),
            "constraints": constraints,
            "can_recommend": can_recommend,
            "basis": "scenario_only" if explicit else "observed_and_forecast",
            "reasons": reasons,
            "warnings": warnings,
            "abstain_reason": reason,
        }


class OptimizationAgent:
    """Evaluates a grid of control moves with the transparent scenario model after the reliability gates."""

    role = "optimization"

    @staticmethod
    def _effort(changes: ControlChanges) -> float:
        """Comparable normalized magnitude of a candidate intervention."""

        return round(
            abs(changes.temperature) / 10
            + abs(changes.feed_rate_pct) / 10
            + abs(changes.pressure) / 2,
            4,
        )

    @staticmethod
    def _gate(
        scenario: dict[str, Any], context: AgentContext,
        reliability: dict[str, Any], quality: dict[str, Any],
    ) -> dict[str, Any]:
        request = context.request
        checks = []
        reasons = list(reliability.get("reasons", []))
        if not reliability.get("can_recommend") and not reasons:
            reasons.append("Проверка надёжности входных данных не пройдена")

        def check(name, passed, reason, basis):
            checks.append({"name": name, "passed": bool(passed), "basis": basis})
            if not passed:
                reasons.append(reason)

        check("sulfur_hard_limit", scenario["hard_sulfur_limit_met"],
              "Сера в конце горизонта превышает обязательный предел 10 мг/кг", "confirmed_10_mg_kg")
        check("sulfur_editable_target", scenario["sulfur_target_met"],
              "Цель по сере не достигнута в выбранном горизонте", "editable_target")
        if scenario.get("exceedance_probability") is not None:
            check("sulfur_exceedance_probability", scenario["exceedance_target_met"],
                  f"Вероятность превышения 10 мг/кг в конце горизонта {scenario['exceedance_probability']:.0%} выше допустимой "
                  f"{request.targets.max_exceedance_probability:.0%}", "editable_probability_target_from_forecast_residuals")
        elif reliability.get("basis") != "scenario_only":
            checks.append({"name": "sulfur_exceedance_probability", "passed": None, "basis": "not_assessed_no_forecast"})
        blend = scenario.get("blend")
        if blend is not None:
            check("blend_sulfur_hard_limit", blend["sulfur"] <= HARD_SULFUR_MAX,
                  "Расчётная сера смеси превышает обязательный предел 10 мг/кг", "confirmed_10_mg_kg")
            for name, passed in blend["meets_targets"].items():
                check(f"blend_{name}_target", passed,
                      f"Смесь не выполняет ограничение {name}", "editable_blend_surrogate")
        else:
            # The hydro-treatment surrogate has no validated T95/cetane effect.
            # Known off-spec values must not be declared remedied by lowering S.
            for name, maximum in (("t95", min(HARD_T95_MAX, request.targets.t95_max)), ("cetane", max(HARD_CETANE_MIN, request.targets.cetane_min))):
                item = quality.get("evidence", {}).get("other_quality", {}).get(name, {})
                value = item.get("value")
                if value is None:
                    checks.append({"name": name, "passed": None, "basis": "not_assessed_missing_quality_evidence"})
                    if reliability.get("basis") != "scenario_only":
                        reasons.append(f"Нет обязательного показателя качества {name}; допустимость наблюдаемого режима не подтверждена")
                    continue
                if not item.get("source", "").startswith("request."):
                    failures = _evidence_failures(name, item, request.at, item.get("max_age_minutes"))
                    reasons.extend(failures)
                    checks.append({"name": f"{name}_evidence", "passed": not failures,
                                   "basis": "slow_quality_window" if item.get("max_age_minutes") else "observed_baseline"})
                check(name, value <= maximum if name == "t95" else value >= maximum,
                      f"Базовый показатель {name} не выполняет ограничение; его отклик не моделируется",
                      "editable_target_baseline_only")
        unassessed = [item["name"] for item in checks if item["passed"] is None]
        return {
            "passed": not reasons,
            "reasons": list(dict.fromkeys(reasons)),
            "checks": checks,
            "scope": "horizon_endpoint_surrogate_and_supplied_blend",
            "unassessed": unassessed,
            "operational_safety_validated": False,
            "forecast_alarm": bool(quality.get("evidence", {}).get("model_forecast", {}) and
                                   quality["evidence"]["model_forecast"].get("alarm_above_10")),
        }

    def _candidates(
        self, context: AgentContext, reliability: dict[str, Any], quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        request = context.request
        # First calculate the editable automatic action.  The grid candidates
        # are evaluated with the same transparent model, so their comparison is
        # deterministic and does not claim an independently validated policy.
        forecast = quality.get("evidence", {}).get("model_forecast")
        automatic_request = request.model_copy(deep=True)
        automatic_request.changes = None
        try:
            automatic = calculate_scenario(context.directory, automatic_request, frame=context.frame, forecast=forecast)
        except ValueError:
            automatic = None
        automatic_changes = ControlChanges(
            temperature=automatic["controls"]["ht.T6"]["change"],
            feed_rate_pct=automatic["controls"]["ht.F9"]["change"],
            pressure=automatic["controls"]["ht.P13"]["change"],
        ) if automatic else None
        current = {mid.split(".")[1]: (quality["evidence"]["controls"].get(mid) or {}).get("value") for mid in CONTROL_IDS}
        grid = candidate_grid(automatic_changes, request.changes, reliability.get("constraints"), current)
        candidates = []
        for definition in grid:
            candidate_id, label, changes = definition["id"], definition["label"], definition["changes"]
            candidate_request = request.model_copy(deep=True)
            candidate_request.changes = changes
            try:
                scenario = calculate_scenario(context.directory, candidate_request, frame=context.frame, forecast=forecast)
            except ValueError as exc:
                candidates.append(
                    {
                        "id": candidate_id,
                        "label": label,
                        "origin": definition["origin"],
                        "status": "error",
                        "feasible": False,
                        "reason": str(exc),
                        "safety_gate": {"passed": False, "reasons": [str(exc)], "checks": []},
                        "scenario": None,
                    }
                )
                continue
            target_met = bool(scenario["sulfur_target_met"])
            effort = self._effort(changes)
            gate = self._gate(scenario, context, reliability, quality)
            risk = risk_assessment({key: value["recommended"] for key, value in scenario["controls"].items()},
                                   {mid: (_values(context.frame).get(mid) or {}).get("value") for mid in _values(context.frame)},
                                   context.model_entry)
            objectives = candidate_objectives(scenario, effort, request.targets.max_exceedance_probability, risk,
                                              min(request.targets.sulfur_max, HARD_SULFUR_MAX))
            risk_passed = risk.get("status") == "ok" and (risk.get("severity") or {}).get("within_model_limit") is True
            gate["checks"].append({"name": "regime_risk_model_limit", "passed": risk_passed,
                                   "basis": "train_support_edge_of_forecast_not_industrial_limit"})
            if not risk_passed:
                gate["reasons"].append("Индекс нагрузки недоступен или превышает экспериментальный предел (край обучающего режима)")
                gate["passed"] = False
            for violation in definition["constraint_violations"]:
                gate["checks"].append({"name": "reliability_constraint", "passed": False, "basis": "reliability_agent_constraint"})
                gate["reasons"].append(violation)
                gate["passed"] = False
            candidates.append(
                {
                    "id": candidate_id,
                    "label": label,
                    "origin": definition["origin"],
                    "status": "ok",
                    "feasible": gate["passed"],
                    "reason": None if gate["passed"] else "; ".join(gate["reasons"]),
                    "predicted_sulfur": scenario["predicted_sulfur"],
                    "predicted_sulfur_lower": scenario.get("predicted_sulfur_lower"),
                    "predicted_sulfur_upper": scenario.get("predicted_sulfur_upper"),
                    "exceedance_probability": scenario.get("exceedance_probability"),
                    "steady_state_sulfur": scenario["steady_state_sulfur"],
                    "target_met": target_met,
                    "effort": effort,
                    "objectives": objectives,
                    "risk": risk,
                    "controls": scenario["controls"],
                    "safety_gate": gate,
                    "scenario": scenario,
                }
            )
        feasible = [item for item in candidates if item.get("feasible", False)]
        infeasible = [item for item in candidates if not item.get("feasible", False)]
        front = pareto_front(feasible)
        for item in infeasible:
            item["pareto"] = False
            item["dominated_by"] = []
        # Feasible candidates win first, the Pareto front before dominated
        # ones; within the front the weighted loss, then the smallest
        # intervention, then the lowest predicted sulphur.
        feasible.sort(
            key=lambda item: (
                0 if item["pareto"] else 1,
                item["objectives"]["ranking_loss"] if item["objectives"]["ranking_loss"] is not None else float("inf"),
                item.get("effort", float("inf")),
                item.get("predicted_sulfur", float("inf")),
            )
        )
        # When the target is unreachable, show the lowest predicted quality
        # first so the operator can see the strongest escalation candidate.
        infeasible.sort(
            key=lambda item: (
                item.get("predicted_sulfur", float("inf")),
                item.get("effort", float("inf")),
            )
        )
        return feasible + infeasible, [item["id"] for item in front]

    def run(
        self,
        context: AgentContext,
        reliability: dict[str, Any],
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        quality = quality or {}
        if quality.get("missing"):
            return {
                "role": self.role,
                "status": "skipped",
                "summary": "Расчёт пропущен: отсутствует базовое качество",
                "candidates": [],
                "pareto_front": [],
                "safety_gate": {"passed": False, "reasons": reliability.get("reasons", []), "checks": []},
                "scenario": None,
            }
        try:
            candidates, front = self._candidates(context, reliability, quality)
        except ValueError as exc:
            return {
                "role": self.role,
                "status": "error",
                "summary": "Сценарная модель вернула ошибку",
                "error": str(exc),
                "candidates": [],
                "pareto_front": [],
                "scenario": None,
            }
        feasible = [item for item in candidates if item.get("feasible")]
        hold = next((item for item in candidates if item["id"] == "hold"), None)
        requires_action = bool((quality.get("problem") or {}).get("requires_action"))
        selection_rule = None
        selected = None
        if not requires_action and hold is not None and hold.get("feasible"):
            # Stable period: no unnecessary control action (organisers' demo requirement).
            selected, selection_rule = hold, "stable_period_hold"
        elif feasible:
            selected, selection_rule = feasible[0], "min_ranking_loss_on_pareto_front"
        if selected is None or selected.get("scenario") is None:
            return {
                "role": self.role,
                "status": "insufficient",
                "summary": "Ни один кандидат не прошёл все применимые проверки; требуется ручной разбор",
                "candidates": candidates,
                "pareto_front": front,
                "requires_action": requires_action,
                "safety_gate": {
                    "passed": False,
                    "reasons": list(dict.fromkeys(reason for item in candidates
                                                   for reason in item.get("safety_gate", {}).get("reasons", []))),
                    "checks": [],
                    "operational_safety_validated": False,
                },
                "scenario": None,
            }
        alternatives = [item for item in feasible if item is not selected and item.get("pareto")][:3]
        result = selected["scenario"]
        model_forecast = (quality or {}).get("evidence", {}).get("model_forecast")
        summary = (f"Кандидатов: {len(candidates)}, допустимых: {len(feasible)}, на фронте Парето: {len(front)}; "
                   f"выбрано: {selected['id']} ({'стабильный период — удержание' if selection_rule == 'stable_period_hold' else 'минимум взвешенных потерь'})")
        return {
            "role": self.role,
            "status": "ok",
            "summary": summary,
            "scenario": result,
            "candidates": candidates,
            "pareto_front": front,
            "requires_action": requires_action,
            "selection_rule": selection_rule,
            "selected_candidate": selected["id"],
            "alternatives": [{"id": item["id"], "label": item["label"], "predicted_sulfur": item["predicted_sulfur"],
                              "exceedance_probability": item.get("exceedance_probability"), "effort": item["effort"],
                              "objectives": item["objectives"], "controls": item["controls"]} for item in alternatives],
            "safety_gate": selected["safety_gate"],
            "model_forecast": model_forecast,
            "recommendation": {
                "action": "review_controls" if selected["id"] != "hold" else "hold",
                "basis": reliability.get("basis", "scenario_only"),
                "requires_operator_review": True,
                "operational_safety_validated": False,
                "predicted_sulfur": result["predicted_sulfur"],
                "predicted_sulfur_lower": result.get("predicted_sulfur_lower"),
                "predicted_sulfur_upper": result.get("predicted_sulfur_upper"),
                "exceedance_probability": result.get("exceedance_probability"),
                "target_sulfur": context.request.targets.sulfur_max,
                "target_met": bool(result["sulfur_target_met"]),
                "controls": result["controls"],
                "candidate_id": selected["id"],
                "candidate_label": selected["label"],
                "objectives": selected["objectives"],
                "risk": selected.get("risk"),
                "model_forecast": model_forecast,
            },
        }


class Orchestrator:
    """Runs the roles in order, resolves their conflicts, checks consistency and assembles the answer."""

    def __init__(self):
        self.quality = QualityAgent()
        self.reliability = ReliabilityAgent()
        self.optimization = OptimizationAgent()

    @staticmethod
    def _conflicts(quality: dict, reliability: dict, optimization: dict) -> list[dict]:
        conflicts = []
        requires_action = bool((quality.get("problem") or {}).get("requires_action"))
        candidates = optimization.get("candidates", [])
        hold = next((c for c in candidates if c["id"] == "hold"), None)
        constraints = reliability.get("constraints") or {}
        if requires_action and reliability.get("can_recommend") and candidates and not any(c.get("feasible") for c in candidates):
            # Candidates that would pass every quality gate but are excluded
            # only by the reliability constraints: the two roles disagree.
            blocked_only = [c for c in candidates if c.get("status") == "ok" and c["id"] != "hold"
                            and all(chk["passed"] is not False or chk["name"] == "reliability_constraint"
                                    for chk in c["safety_gate"]["checks"])
                            and any(chk["name"] == "reliability_constraint" for chk in c["safety_gate"]["checks"])]
            if blocked_only:
                conflicts.append({"code": "quality_vs_reliability", "resolution": "abstain",
                                  "message": "Качество требует снижения серы, но все подходящие изменения заблокированы ограничениями надёжности "
                                             f"({', '.join(c['id'] for c in blocked_only[:5])})"})
        if requires_action and reliability.get("basis") != "scenario_only" \
                and (reliability.get("confidence") or {}).get("class") == "low" and (hold is None or not hold.get("feasible")):
            conflicts.append({"code": "low_confidence_action", "resolution": "abstain",
                              "message": "Требуется корректирующее действие, но уверенность в данных низкая; удержание недопустимо — нужен ручной разбор"})
        selected = next((c for c in candidates if c["id"] == optimization.get("selected_candidate")), None)
        if selected and selected["id"] != "hold" and hold is not None and selected.get("scenario"):
            effect = selected["scenario"].get("control_effect_ln_at_horizon") or 0.0
            sigma = selected["scenario"].get("interval_widening_ln_sigma") or 0.0
            if sigma and abs(effect) < 1.28 * sigma:
                conflicts.append({"code": "effect_uncertain", "resolution": "warning",
                                  "message": "Эффект выбранного изменения неотличим от нуля с учётом неопределённости коэффициентов (80% ДИ)"})
        for metric_id, rule in constraints.items():
            if rule.get("blocked") and selected and selected["id"] != "hold" and selected["controls"][metric_id]["change"]:
                conflicts.append({"code": "blocked_control_selected", "resolution": "abstain",
                                  "message": f"Выбранный кандидат меняет заблокированный тег {metric_id}"})
        return conflicts

    @staticmethod
    def _consistency(request: ScenarioRequest, quality: dict, optimization: dict) -> list[dict]:
        checks = []
        forecast = quality.get("evidence", {}).get("model_forecast") or {}
        candidates = optimization.get("candidates", [])
        hold = next((c for c in candidates if c["id"] == "hold" and c.get("scenario")), None)
        baseline = quality["evidence"]["sulfur"]["value"]

        def check(code, passed, message):
            checks.append({"code": code, "passed": bool(passed), "message": message})

        if hold is not None:
            scenario = hold["scenario"]
            check("K1_baseline_shared", abs((scenario["baseline"]["sulfur"] or 0) - (baseline or 0)) < 1e-9,
                  "База сценария совпадает с оценкой агента качества")
            if forecast.get("status") == "ok" and scenario["baseline"]["baseline_kind"] == "model_forecast" and forecast.get("prediction"):
                check("K1_hold_equals_forecast", abs(scenario["predicted_sulfur"] - forecast["prediction"]) < 1e-6,
                      "Кандидат hold совпадает с прогнозом без воздействия")
            signs = {"ht.T6": -1, "ht.F9": 1, "ht.P13": -1}
            ok = True
            for c in candidates:
                if not c.get("scenario"):
                    continue
                for metric_id, sign in signs.items():
                    effect = c["controls"][metric_id].get("effect_ln_at_horizon") or 0.0
                    change = c["controls"][metric_id]["change"]
                    if change and effect and (effect > 0) != ((change * sign) > 0):
                        ok = False
            check("K2_effect_signs", ok, "Знаки эффектов кандидатов соответствуют направлению коэффициентов")
            check("K4_same_origin", all(c["scenario"]["at"] == request.at for c in candidates if c.get("scenario"))
                  and (not forecast.get("feature_time") or parse_time(forecast["feature_time"]) <= parse_time(request.at)),
                  "Все кандидаты рассчитаны на один момент, признаки прогноза не позже момента решения")
        if forecast:
            check("K5_leakage", (forecast.get("leakage_check") or {}).get("passed", True), "Проверка отсутствия утечки будущего в прогнозе")
        return checks

    def decide(self, directory: Path, request: ScenarioRequest) -> dict[str, Any]:
        frame = snapshot(directory, request.at)
        try:
            _, model_entry = applicable_model(request.at)
        except ForecastUnavailable:
            model_entry = None
        context = AgentContext(directory=directory, request=request, frame=frame, model_entry=model_entry)
        quality = self.quality.run(context)
        reliability = self.reliability.run(context, quality)
        optimization = self.optimization.run(context, reliability, quality)
        conflicts = self._conflicts(quality, reliability, optimization)
        consistency = self._consistency(request, quality, optimization)
        trace = [
            {"step": 1, "role": quality["role"], "status": quality["status"], "summary": quality["summary"],
             "consumes": ["snapshot", "forecast"], "produces": ["sulfur baseline", "problem", "other quality", "controls evidence"]},
            {"step": 2, "role": reliability["role"], "status": reliability["status"], "summary": reliability["summary"],
             "consumes": ["quality.evidence", "forecast.applicability", "model support"],
             "produces": ["gates", "risk index", "constraints", "confidence"]},
            {"step": 3, "role": optimization["role"], "status": optimization["status"], "summary": optimization["summary"],
             "consumes": ["quality.problem", "reliability.constraints", "reliability.risk", "scenario model"],
             "produces": ["candidates", "pareto front", "selection", "alternatives"]},
            {"step": 4, "role": "orchestrator", "status": "abstain" if any(c["resolution"] == "abstain" for c in conflicts)
             or not all(c["passed"] for c in consistency) else "ok",
             "summary": f"Конфликтов: {len(conflicts)}, проверок согласованности: {sum(c['passed'] for c in consistency)}/{len(consistency)}",
             "consumes": ["quality", "reliability", "optimization"], "produces": ["recommendation | abstain", "explanation"]},
        ]
        abstain_reason = reliability.get("abstain_reason")
        if optimization["status"] == "error":
            abstain_reason = optimization.get("error")
        elif optimization["status"] != "ok" and not abstain_reason:
            abstain_reason = optimization["summary"]
        conflict_abstain = [c["message"] for c in conflicts if c["resolution"] == "abstain"]
        failed_checks = [c["message"] for c in consistency if not c["passed"]]
        if not abstain_reason and optimization["status"] == "ok" and conflict_abstain:
            abstain_reason = "Конфликт целей: " + "; ".join(conflict_abstain)
        if not abstain_reason and optimization["status"] == "ok" and failed_checks:
            abstain_reason = "Внутренняя несогласованность агентов: " + "; ".join(failed_checks)
        abstained = bool(abstain_reason) or optimization["status"] != "ok"
        safety_gate = optimization.get("safety_gate", {"passed": False, "reasons": [abstain_reason], "checks": []})
        if abstained and safety_gate.get("passed"):
            # The optimiser found an admissible candidate but the orchestrator
            # refused it (conflict or failed consistency): the published gate
            # must reflect the refusal.
            safety_gate = {**safety_gate, "passed": False, "reasons": [abstain_reason, *safety_gate.get("reasons", [])],
                           "orchestrator_override": True}
        decision = {
            "at": request.at,
            "status": "abstain" if abstained else "recommendation",
            "basis": reliability["basis"],
            "scope": "scenario_review_only",
            "operational_safety_validated": False,
            "recommendation": None if abstained else optimization.get("recommendation"),
            "scenario": None if abstained else optimization.get("scenario"),
            "candidates": optimization.get("candidates", []),
            "pareto_front": optimization.get("pareto_front", []),
            "alternatives": [] if abstained else optimization.get("alternatives", []),
            "selected_candidate": None if abstained else optimization.get("selected_candidate"),
            "selection_rule": None if abstained else optimization.get("selection_rule"),
            "safety_gate": safety_gate,
            "forecast": quality.get("evidence", {}).get("model_forecast"),
            "confidence": reliability.get("confidence"),
            "risk": reliability.get("risk"),
            "constraints": reliability.get("constraints"),
            "problem": quality.get("problem"),
            "conflicts": conflicts,
            "consistency": consistency,
            "abstain": (
                {
                    "reason": abstain_reason or optimization["summary"],
                    "missing": quality.get("missing", []),
                }
                if abstained
                else None
            ),
            "agents": {
                "quality": quality,
                "reliability": reliability,
                "optimization": optimization,
            },
            "trace": trace,
            "assumptions": [
                (
                    "Все агенты работают локально по детерминированным правилам; обмен данными между ролями "
                    "показан в trace (consumes/produces)."
                ),
                (
                    "Рекомендация основана на snapshot без будущих измерений и "
                    "на мультипликативной сценарной модели поверх двухступенчатого прогноза."
                ),
                (
                    "База качества — калиброванный по ЛИМС nowcast анализатора; путь без воздействия учитывает уже сделанные "
                    "изменения режима; интервал и вероятность превышения получены из эмпирических остатков модели, "
                    "расширенных на неопределённость коэффициентов. Эффект изменения режима — сценарная модель с "
                    "наблюдательными коэффициентами; он не заменяет проверку технологом."
                ),
                (
                    "Ограничения агента надёжности — край обучающего режима модели и шаговые пределы прототипа, "
                    "не паспортные пределы. Индекс риска и уверенность — эвристические прокси, не вероятности."
                ),
                (
                    "Результат предназначен для рассмотрения оператором. Проверяются "
                    "конечная точка сценария и заданная смесь; технологические границы, "
                    "скорости изменения и причинные коэффициенты не подтверждены. "
                    "Полная безопасность режима не доказана."
                ),
            ],
        }
        decision["explanation"] = build_explanation(decision)
        return decision


def make_decision(directory: Path, request: ScenarioRequest) -> dict[str, Any]:
    """Convenience function used by the API and by offline tests."""

    return Orchestrator().decide(directory, request)
