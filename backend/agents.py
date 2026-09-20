"""Deterministic multi-agent decision support for the local prototype.

The agents in this module are deliberately small, transparent Python rules.  They
provide the same separation of concerns as a future LLM or service based setup,
while keeping every decision reproducible in the closed hackathon network.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

from .analytics import parse_time, snapshot
from .scenarios import HARD_SULFUR_MAX, HARD_T95_MAX, HARD_CETANE_MIN, ControlChanges, ScenarioRequest, calculate_scenario, select_sulfur
from .forecast import ForecastUnavailable, forecast_sulfur
from .objectives import candidate_objectives, regime_severity

CONTROL_IDS = ("ht.T6", "ht.F9", "ht.P13")
UNUSABLE_FLAGS = {"invalid", "conflict", "suspect", "flatline", "gap"}


@dataclass(frozen=True)
class AgentContext:
    directory: Path
    request: ScenarioRequest
    frame: dict[str, Any]


def _values(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["metric_id"]: item for item in frame.get("values", [])}


def _evidence(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {"value": None, "timestamp": None, "freshness": "missing", "flags": []}
    return {
        "value": item.get("value"),
        "timestamp": item.get("timestamp"),
        "freshness": item.get("freshness"),
        "flags": item.get("flags", []),
        "available_at": item.get("available_at"),
    }


def _evidence_failures(label: str, item: dict[str, Any], at: str) -> list[str]:
    """Hard gates; a confidence score cannot compensate for a bad signal."""
    reasons = []
    value = item.get("value")
    if value is None or not isfinite(value):
        reasons.append(f"{label}: отсутствует конечное численное значение")
    if item.get("freshness") != "fresh":
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
    """Checks that the snapshot contains a usable quality baseline."""

    role = "quality"

    def run(self, context: AgentContext) -> dict[str, Any]:
        values = _values(context.frame)
        request = context.request
        if request.current_sulfur is not None:
            sulfur = request.current_sulfur
            sulfur_source = "request.current_sulfur"
            sulfur_item = None
        else:
            sulfur, sulfur_source = select_sulfur(values)
            sulfur_item = values.get(sulfur_source) if sulfur_source else None

        controls = {metric_id: _evidence(values.get(metric_id)) for metric_id in CONTROL_IDS}
        available_controls = [
            metric_id for metric_id, evidence in controls.items() if evidence["value"] is not None
        ]
        missing = [] if sulfur is not None else ["sulfur_baseline"]
        status = "ok" if not missing else "insufficient"
        warnings = []
        if sulfur_item and sulfur_item.get("freshness") == "stale":
            warnings.append("Базовое измерение серы устарело")
        if not available_controls:
            warnings.append("Нет текущих значений управляющих тегов T6/F9/P13")

        model_forecast = None
        try:
            model_forecast = forecast_sulfur(context.directory, request.at, horizon_minutes=request.horizon_minutes)
        except ForecastUnavailable as exc:
            warnings.append(f"Прогноз серы недоступен: {exc}")

        if (request.current_sulfur is None and model_forecast and model_forecast.get("status") == "ok"
                and model_forecast.get("path_supported") and model_forecast.get("nowcast")):
            sulfur, sulfur_source = float(model_forecast["nowcast"]["prediction"]), "model.nowcast"
            sulfur_item = {"timestamp": request.at, "available_at": request.at,
                           "freshness": "fresh", "flags": []}
            missing, status = [], "ok"

        if request.current_sulfur is not None:
            warnings.append("Базовая сера введена вручную: это условный сценарий, а не подтверждение качества продукта")
        if model_forecast and model_forecast.get("status") == "abstain":
            warnings.extend(model_forecast.get("reasons", []))
        if model_forecast and model_forecast.get("alarm_above_10"):
            warnings.append("Прогноз без воздействия сигнализирует о риске превышения 10 мг/кг; сценарные коэффициенты не доказывают устранение этого риска")

        other_quality = {}
        for name, supplied, metric_id in (
            ("t95", request.current_t95, "lims.ht.2.95%.T"),
            ("cetane", request.current_cetane, "lims.ht.2.CetaneNumber"),
        ):
            other_quality[name] = (
                {"source": f"request.current_{name}", "value": supplied}
                if supplied is not None
                else {"source": metric_id, **_evidence(values.get(metric_id))}
            )
        analyser_rows = [values.get(mid, {}) for mid in ("pak.ht.Mg.Sulfur", "ht.Q21")]
        comparable = all(item.get("value") is not None and isfinite(item["value"]) and item.get("freshness") == "fresh"
                         and not UNUSABLE_FLAGS.intersection(item.get("flags") or []) for item in analyser_rows)
        difference = abs(analyser_rows[0]["value"]-analyser_rows[1]["value"]) if comparable else None
        comparison = {"ids": ["pak.ht.Mg.Sulfur", "ht.Q21"], "difference_mgkg": difference,
                      "conflict": difference is not None and difference > 2,
                      "threshold_mgkg": 2, "basis": "engineering disagreement threshold, not calibrated accuracy"}
        pressure_drop = {"source": "ht.P8", **_evidence(values.get("ht.P8")),
                         "unit": values.get("ht.P8", {}).get("unit"),
                         "change_from_previous": values.get("ht.P8", {}).get("delta"),
                         "industrial_limit": None, "failure_probability": None,
                         "interpretation": "Measured reactor pressure drop; limits and failure relation are not identified"}

        return {
            "role": self.role,
            "status": status,
            "summary": (
                "Базовое качество доступно для расчёта"
                if status == "ok"
                else "Невозможно определить базовую серу"
            ),
            "evidence": {
                # Put the explicit request value after evidence so a missing
                # telemetry item cannot overwrite it with ``None``.
                "sulfur": {"source": sulfur_source, **_evidence(sulfur_item), "value": sulfur},
                "controls": controls,
                "available_control_count": len(available_controls),
                "model_forecast": model_forecast,
                "other_quality": other_quality,
                "analyzer_comparison": comparison,
                "reactor_pressure_drop": pressure_drop,
                "blend_quality_basis": "user_supplied_scenario_properties" if request.tanks else None,
            },
            "warnings": warnings,
            "missing": missing,
        }


class ReliabilityAgent:
    """Applies freshness and availability gates before an action is proposed."""

    role = "reliability"

    def run(self, context: AgentContext, quality: dict[str, Any]) -> dict[str, Any]:
        sulfur = quality["evidence"]["sulfur"]
        explicit = sulfur["source"] == "request.current_sulfur"
        missing = list(quality.get("missing", []))
        warnings = list(quality.get("warnings", []))
        reasons = ["Недостаточно данных: отсутствует базовое значение серы"] if missing else []
        if not explicit:
            reasons.extend(_evidence_failures("Базовая сера", sulfur, context.request.at))
        # All three controls define the intervention vector.  Missing, stale or
        # flagged inputs block it even when another input has high confidence.
        for metric_id, evidence in quality["evidence"]["controls"].items():
            reasons.extend(_evidence_failures(metric_id, evidence, context.request.at))
        forecast = quality["evidence"].get("model_forecast")
        if not explicit:
            if not forecast or forecast.get("status") != "ok":
                reasons.append("Нет допустимого модельного прогноза для решения по наблюдаемым данным")
            elif not forecast.get("path_supported", True):
                reasons.append("Не все горизонты траектории прошли проверку области применимости")
            elif forecast.get("alarm_above_10") and not context.request.tanks:
                reasons.append("Независимый прогноз указывает превышение 10 мг/кг; действие требует отдельной проверки")
            if forecast and forecast.get("horizon_minutes", forecast.get("forecast_horizon_minutes", 180)) != context.request.horizon_minutes:
                reasons.append("Горизонт сценария не совпадает с горизонтом независимой модели; прогноз не подтверждает эту конечную точку")
        analyzers = quality.get("evidence", {}).get("analyzer_comparison", {})
        if not explicit and analyzers.get("conflict"):
            reasons.append("Свежие поточные анализаторы серы расходятся; требуется проверка источников")
        can_recommend = not reasons
        severity = regime_severity({key: item.get("value") for key, item in quality["evidence"]["controls"].items()})
        # Compatibility field only: this is a gate score, not a calibrated
        # confidence, reliability probability, or probability of safe product.
        confidence = 1.0 if can_recommend else 0.0
        reason = "; ".join(reasons) if reasons else None
        return {
            "role": self.role,
            "status": "ok" if can_recommend else "insufficient",
            "summary": (
                "Временная доступность и свежесть прошли контроль"
                if can_recommend
                else reason
            ),
            "confidence": confidence,
            "confidence_kind": "binary_data_gate_not_probability",
            "regime_severity": severity,
            "can_recommend": can_recommend,
            "basis": "scenario_only" if explicit else "observed_and_forecast",
            "reasons": reasons,
            "warnings": warnings,
            "abstain_reason": reason,
        }


class OptimizationAgent:
    """Runs the existing transparent scenario model after reliability gates."""

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

        check("sulfur_hard_limit", scenario["product_sulfur"] <= HARD_SULFUR_MAX,
              "Сера в конце горизонта превышает обязательный предел 10 мг/кг", "confirmed_10_mg_kg")
        check("sulfur_editable_target", scenario["sulfur_target_met"],
              "Цель по сере не достигнута в выбранном горизонте", "editable_target")
        blend = scenario.get("blend")
        check("intermediate_sulfur_limit", scenario["intermediate_sulfur_limit_met"],
              "Нарушен отдельно заданный предел гидроочистки", "explicit_intermediate_limit")
        if blend is not None:
            if not any(t.kind == "hydrotreated_batch" for t in request.tanks):
                check("stored_blend_controls", all(c["change"] == 0 for c in scenario["controls"].values()),
                      "Изменение гидроочистки не связано с запасённой смесью; задайте поступление новой партии",
                      "no_upstream_control_credit_for_stored_inventory")
            check("blend_sulfur_hard_limit", blend["sulfur"] <= HARD_SULFUR_MAX,
                  "Расчётная сера смеси превышает обязательный предел 10 мг/кг", "confirmed_10_mg_kg")
            for name, passed in blend["meets_targets"].items():
                check(f"blend_{name}_target", passed,
                      f"Смесь не выполняет ограничение {name}", "editable_blend_surrogate")
            check("blend_stock", blend["stock_constraints_met"] is not False,
                  "Недостаточно запаса компонента для массы партии", "component_mass_inventory")
            if blend["stock_constraints_met"] is None:
                checks.append({"name": "blend_stock_assessed", "passed": None, "basis": "missing_batch_mass_or_stock"})
                if reliability.get("basis") != "scenario_only":
                    reasons.append("Не заданы масса партии или запасы; доступность смеси не подтверждена")
            check("additive_sulfur_assessed", blend["additive_sulfur_assessed"],
                  "Не задано содержание серы в присадке", "explicit_additive_composition")
            # Keep the independent warning. Propagate it through the same mass balance,
            # without crediting an unvalidated beneficial control response.
            forecast = quality.get("evidence", {}).get("model_forecast") or {}
            if reliability.get("basis") != "scenario_only" and any(t.kind == "hydrotreated_batch" for t in request.tanks):
                # A conservative envelope across the arriving segment, not the H3 endpoint
                # used as if it described all material. No probability claim for batch quality.
                guard = forecast.get("prediction_risk_guard", forecast.get("prediction_ridge"))
                if forecast.get("path_supported") and forecast.get("nowcast"):
                    from .scenarios import _at_knots
                    horizon = request.horizon_minutes
                    upper_knots = sorted([(float(r["minutes"]), float(r["upper"])) for r in forecast["horizons"]
                                          if r["minutes"] < horizon and r.get("upper") is not None]
                                         + [(float(horizon), float(forecast["prediction_upper"]))])
                    end = scenario["batch"]["arriving_minutes"]
                    guard = max([v for t, v in upper_knots if t <= end] + [_at_knots(upper_knots, end)])
                if guard is None:
                    check("blend_forecast_guard", False, "Нет независимой оценки качества поступающей партии", "forecast_mass_balance")
                else:
                    from .scenarios import _blend
                    worst = max(float(guard), scenario["baseline"]["sulfur"], scenario["batch"]["produced_sulfur"])
                    guard_blend = _blend(request, produced_sulfur=worst, produced_mass=scenario["batch"]["produced_t"])
                    check("blend_forecast_guard", guard_blend["sulfur"] <= min(HARD_SULFUR_MAX, request.targets.sulfur_max),
                          "Независимая оценка поступающей партии нарушает предел конечной смеси", "heuristic_guard_not_probability")
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
                    failures = _evidence_failures(name, item, request.at)
                    reasons.extend(failures)
                    checks.append({"name": f"{name}_evidence", "passed": not failures, "basis": "observed_baseline"})
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
        # First calculate the editable automatic action.  The other candidates
        # are derived from this same transparent model, so their comparison is
        # deterministic and does not claim an independently validated policy.
        automatic_request = request.model_copy(deep=True)
        automatic_request.changes = None
        try:
            automatic = calculate_scenario(context.directory, automatic_request, frame=context.frame,
                                           forecast=quality.get("evidence", {}).get("model_forecast"))
        except ValueError:
            automatic = None
        # Controls in the scenario result use metric ids; map them explicitly
        # to the public request fields to keep this adapter independent of dict
        # ordering and future control additions.
        automatic_changes = ControlChanges(
            temperature=automatic["controls"]["ht.T6"]["change"],
            feed_rate_pct=automatic["controls"]["ht.F9"]["change"],
            pressure=automatic["controls"]["ht.P13"]["change"],
        ) if automatic else ControlChanges()
        definitions = [
            ("hold", "Удержать текущий режим", ControlChanges()),
            (
                "conservative",
                "Консервативное изменение (50% от automatic)",
                ControlChanges(
                    temperature=automatic_changes.temperature * 0.5,
                    feed_rate_pct=automatic_changes.feed_rate_pct * 0.5,
                    pressure=automatic_changes.pressure * 0.5,
                ),
            ),
            ("automatic", "Изменение до цели в рамках модели", automatic_changes),
        ]
        if request.changes is not None:
            definitions.append(("requested", "Изменение, заданное пользователем", request.changes))
        if request.optimize_economics:
            definitions.extend([
                ("lower_heat", "Снизить нагрев на 2 °C", ControlChanges(temperature=-2)),
                ("more_feed", "Увеличить подачу на 2%", ControlChanges(feed_rate_pct=2)),
                ("lower_pressure", "Снизить давление на 0,1 МПа", ControlChanges(pressure=-.1)),
            ])
        if request.tanks and not any(t.kind == "hydrotreated_batch" for t in request.tanks):
            definitions = [item for item in definitions if item[0] in {"hold", "requested"}]
        recipes = [("", request.tanks, request.additive_pct)]
        if request.optimize_recipe and request.tanks:
            # Local, bounded recipe alternatives; shares remain exactly normalized.
            for donor in range(len(request.tanks)):
                for receiver in range(len(request.tanks)):
                    if donor == receiver or request.tanks[donor].share < 5:
                        continue
                    tanks = [t.model_copy(deep=True) for t in request.tanks]
                    tanks[donor].share -= 5; tanks[receiver].share += 5
                    recipes.append((f"_mix{donor}_{receiver}", tanks, request.additive_pct))
            for dose in sorted({0., max(0., request.additive_pct-.25), min(3., request.additive_pct+.25)}):
                if dose != request.additive_pct:
                    recipes.append((f"_dose{dose:g}", request.tanks, dose))
        candidates = []
        combinations = [(i+s, label+(f"; доли {'/'.join(f'{t.share:g}' for t in tanks)}%, присадка {dose*10:g} кг/т" if s else ""), changes, tanks, dose)
                        for i,label,changes in definitions for s,tanks,dose in recipes]
        for candidate_id, label, changes, tanks, dose in combinations:
            candidate_request = request.model_copy(deep=True)
            candidate_request.changes = changes
            candidate_request.tanks = tanks
            candidate_request.additive_pct = dose
            candidate_context = AgentContext(context.directory, candidate_request, context.frame)
            try:
                scenario = calculate_scenario(context.directory, candidate_request, frame=context.frame,
                                              forecast=quality.get("evidence", {}).get("model_forecast"))
            except ValueError as exc:
                candidates.append(
                    {
                        "id": candidate_id,
                        "label": label,
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
            gate = self._gate(scenario, candidate_context, reliability, quality)
            objectives = candidate_objectives(scenario, effort)
            severity = objectives["regime_severity"]
            severity_passed = severity.get("status") == "ok" and severity.get("within_model_limit") is True
            gate["checks"].append({"name": "regime_severity_model_limit", "passed": severity_passed,
                                   "basis": "experimental_high_side_12sigma_not_industrial_limit"})
            if not severity_passed:
                gate["reasons"].append("Индекс нагрузки недоступен или превышает экспериментальный предел")
                gate["passed"] = False
            candidates.append(
                {
                    "id": candidate_id,
                    "label": label,
                    "status": "ok",
                    "feasible": gate["passed"],
                    "reason": None if gate["passed"] else "; ".join(gate["reasons"]),
                    "predicted_sulfur": scenario["predicted_sulfur"],
                    "product_sulfur": scenario["product_sulfur"],
                    "steady_state_sulfur": scenario["steady_state_sulfur"],
                    "target_met": target_met,
                    "effort": effort,
                    "objectives": objectives,
                    "controls": scenario["controls"],
                    "safety_gate": gate,
                    "scenario": scenario,
                }
            )
        # Feasible candidates win first; rank by the declared multi-objective
        # loss, then intervention and final product sulfur. Failed candidates
        # remain diagnostic evidence; none is eligible for a recommendation.
        feasible = [item for item in candidates if item.get("feasible", False)]
        infeasible = [item for item in candidates if not item.get("feasible", False)]
        feasible.sort(
            key=lambda item: (
                item["objectives"]["ranking_loss"],
                item.get("effort", float("inf")),
                item.get("product_sulfur", float("inf")),
            )
        )
        hold = next((item for item in feasible if item["id"] == "hold"), None)
        if hold and feasible and hold["objectives"]["ranking_loss"] - feasible[0]["objectives"]["ranking_loss"] < request.minimum_economic_gain:
            feasible.remove(hold); feasible.insert(0, hold)
        # When the target is unreachable, show the lowest predicted quality
        # first so the operator can see the strongest escalation candidate.
        infeasible.sort(
            key=lambda item: (
                item.get("product_sulfur", float("inf")),
                item.get("effort", float("inf")),
            )
        )
        candidates = feasible + infeasible
        return candidates

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
                "safety_gate": {"passed": False, "reasons": reliability.get("reasons", []), "checks": []},
                "scenario": None,
            }
        try:
            candidates = self._candidates(context, reliability, quality)
        except ValueError as exc:
            return {
                "role": self.role,
                "status": "error",
                "summary": "Сценарная модель вернула ошибку",
                "error": str(exc),
                "scenario": None,
            }
        selected = next((candidate for candidate in candidates if candidate.get("feasible")), None)
        if selected is None or selected.get("scenario") is None:
            return {
                "role": self.role,
                "status": "insufficient",
                "summary": "Ни один кандидат не прошёл все применимые проверки; требуется ручной разбор",
                "candidates": candidates,
                "safety_gate": {
                    "passed": False,
                    "reasons": list(dict.fromkeys(reason for item in candidates
                                                   for reason in item.get("safety_gate", {}).get("reasons", []))),
                    "checks": [],
                    "operational_safety_validated": False,
                },
                "scenario": None,
            }
        result = selected["scenario"]
        predicted = result["product_sulfur"]
        target = result["sulfur_target_met"]
        model_forecast = (quality or {}).get("evidence", {}).get("model_forecast")
        summary = (
            "Расчётный кандидат проходит применимые проверки для рассмотрения оператором"
            if target
            else "Сценарий не достигает цели по сере; требуется ручная проверка"
        )
        summary = (
            f"{summary}; кандидатов: {len(candidates)}, выбрано: {selected['id']}, "
            f"safety gate: {'passed' if selected['safety_gate']['passed'] else 'failed'}"
        )
        return {
            "role": self.role,
            "status": "ok",
            "summary": summary,
            "scenario": result,
            "candidates": candidates,
            "selected_candidate": selected["id"],
            "safety_gate": selected["safety_gate"],
            "model_forecast": model_forecast,
            "recommendation": {
                "action": "review_controls",
                "basis": reliability.get("basis", "scenario_only"),
                "requires_operator_review": True,
                "operational_safety_validated": False,
                "predicted_sulfur": predicted,
                "target_sulfur": context.request.targets.sulfur_max,
                "target_met": target,
                "controls": result["controls"],
                "product_route": result["product_route"],
                "applied_recipe": result["applied_recipe"],
                "candidate_id": selected["id"],
                "objectives": selected["objectives"],
                "model_forecast": model_forecast,
            },
        }


class Orchestrator:
    """Runs agents in a fixed order and returns an explainable execution trace."""

    def __init__(self):
        self.quality = QualityAgent()
        self.reliability = ReliabilityAgent()
        self.optimization = OptimizationAgent()

    def decide(self, directory: Path, request: ScenarioRequest) -> dict[str, Any]:
        frame = snapshot(directory, request.at)
        context = AgentContext(directory=directory, request=request, frame=frame)
        quality = self.quality.run(context)
        reliability = self.reliability.run(context, quality)
        optimization = self.optimization.run(context, reliability, quality)
        trace = [
            {
                "step": index,
                "role": result["role"],
                "status": result["status"],
                "summary": result["summary"],
            }
            for index, result in enumerate((quality, reliability, optimization), start=1)
        ]
        abstain_reason = reliability.get("abstain_reason")
        if optimization["status"] == "error":
            abstain_reason = optimization.get("error")
        elif optimization["status"] != "ok" and not abstain_reason:
            abstain_reason = optimization["summary"]
        abstained = bool(abstain_reason) or optimization["status"] != "ok"
        return {
            "at": request.at,
            "status": "abstain" if abstained else "recommendation",
            "basis": reliability["basis"],
            "scope": "scenario_review_only",
            "operational_safety_validated": False,
            "recommendation": None if abstained else optimization.get("recommendation"),
            "scenario": None if abstained else optimization.get("scenario"),
            "candidates": optimization.get("candidates", []),
            "selected_candidate": None if abstained else optimization.get("selected_candidate"),
            "safety_gate": optimization.get("safety_gate", {"passed": False, "reasons": [abstain_reason], "checks": []}),
            "forecast": quality.get("evidence", {}).get("model_forecast"),
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
                    "Все агенты работают локально по детерминированным правилам; "
                    "внешний LLM не вызывается."
                ),
                (
                    "Рекомендация основана на snapshot без будущих измерений и "
                    "на линейной сценарной модели."
                ),
                (
                    "Ridge-прогноз серы показывается как отдельный контрольный "
                    "сигнал; он не доказывает причинный эффект изменения режима "
                    "и не заменяет проверку технологом."
                ),
                (
                    "Результат предназначен для рассмотрения оператором. Проверяются "
                    "конечная точка сценария и заданная смесь; технологические границы, "
                    "скорости изменения и причинные коэффициенты не подтверждены. "
                    "Полная безопасность режима не доказана."
                ),
            ],
        }


def make_decision(directory: Path, request: ScenarioRequest) -> dict[str, Any]:
    """Convenience function used by the API and by offline tests."""

    return Orchestrator().decide(directory, request)
