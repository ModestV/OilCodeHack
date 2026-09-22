"""Deterministic multi-agent decision support for the local prototype.

The agents in this module are deliberately small, transparent Python rules.  They
provide the same separation of concerns as a future LLM or service based setup,
while keeping every decision reproducible in the closed hackathon network.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path
from typing import Any

from .analytics import parse_time, snapshot
from .explain import build_explanation
from .forecast import ForecastUnavailable, forecast_sulfur
from .lab_drift import CETANE_METRIC, aged_lower_bound, drift_sigma
from .llm import explain as llm_explain
from .objectives import annotate_pareto, candidate_objectives, operating_state, regime_severity
from .scenarios import (
    HARD_CETANE_MIN,
    HARD_SULFUR_MAX,
    HARD_T95_MAX,
    MAX_ADDITIVE_PCT,
    STEP_LIMITS,
    ControlChanges,
    ScenarioRequest,
    calculate_scenario,
    select_sulfur,
)

CONTROL_IDS = ("ht.T6", "ht.F9", "ht.P13")
ANALYSER_IDS = ("pak.ht.Mg.Sulfur", "ht.Q21")
UNUSABLE_FLAGS = {"invalid", "conflict", "suspect", "flatline", "gap"}
# Forecast abstain codes in operator language, with what would unblock the decision.
FORECAST_REASONS = {
    "no_sulfur_evidence": "нет поточного анализатора серы (ПАК или Q21) — загрузите выгрузку ПАК",
    "insufficient_lab_anchor": "мало опубликованных проб ЛИМС серы для калибровки анализаторов — загрузите ЛИМС",
    "no_control_telemetry": "нет телеметрии T6/F9/P13",
    "too_many_missing_features": "слишком много пропусков во входах модели",
    "outside_training_support": "режим вне области обучения модели",
    "model_not_yet_available_at_origin": "момент раньше 01.01.2026: модель обучена на этой истории, прогноз был бы утечкой",
    "nonfinite_model_output": "некорректный численный результат модели",
}


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


def _evidence_failures(label: str, item: dict[str, Any], at: str, require_fresh: bool = True) -> list[str]:
    """Hard gates; a confidence score cannot compensate for a bad signal."""
    reasons = []
    value = item.get("value")
    if value is None or not isfinite(value):
        reasons.append(f"{label}: отсутствует конечное численное значение")
    if require_fresh and item.get("freshness") != "fresh":
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


def required_additive(request: ScenarioRequest, quality: dict[str, Any]) -> dict[str, Any] | None:
    """Smallest improver dose (step 0.05%) lifting the cetane lower bound to the limit; direct route only."""
    if request.tanks:
        return None
    item = quality.get("evidence", {}).get("other_quality", {}).get("cetane") or {}
    bound = item.get("lower_bound", item.get("value"))
    if bound is None or not isfinite(bound):
        return None
    minimum = max(HARD_CETANE_MIN, request.targets.cetane_min)
    gain = request.parameters.cetane_gain_per_pct
    gap = minimum - bound
    pct = 0.0 if gap <= 0 else ceil(gap / gain / 0.05 - 1e-9) * 0.05 if gain > 0 else float("inf")
    return {"pct": round(pct, 4), "lower_bound": bound, "minimum": minimum, "gain_per_pct": gain,
            "feasible": pct <= MAX_ADDITIVE_PCT}


class QualityAgent:
    """Checks that the snapshot contains a usable quality baseline."""

    role = "quality"

    @staticmethod
    def _analyser_comparison(values: dict[str, dict[str, Any]], forecast: dict[str, Any] | None) -> dict[str, Any]:
        """PAK vs Q21 after each is anchored to published LIMS.

        The two analysers carry different, slowly varying offsets from the lab
        (Q21 reads ~1–2 mg/kg higher than PAK in 2026).  The forecast already
        estimates these offsets from lab pairs; comparing raw readings would
        report that known bias as a data conflict.
        """
        rows = [values.get(mid, {}) for mid in ANALYSER_IDS]
        comparable = all(item.get("value") is not None and isfinite(item["value"]) and item.get("freshness") == "fresh"
                         and not UNUSABLE_FLAGS.intersection(item.get("flags") or []) for item in rows)
        raw = abs(rows[0]["value"] - rows[1]["value"]) if comparable else None
        analysers = (forecast or {}).get("analysers") or {}
        adjusted = [(analysers.get(name) or {}).get("adjusted") for name in ("pak", "q21")]
        anchored = comparable and all(v is not None and isfinite(v) for v in adjusted)
        difference = abs(adjusted[0] - adjusted[1]) if anchored else raw
        return {"ids": list(ANALYSER_IDS), "difference_mgkg": difference, "raw_difference_mgkg": raw,
                "adjusted_mgkg": dict(zip(ANALYSER_IDS, adjusted)) if anchored else None,
                "conflict": difference is not None and difference > 2, "threshold_mgkg": 2,
                "basis": ("lab-anchored readings (analyser value + offset to recent LIMS)" if anchored
                          else "raw readings: no lab anchor available")
                         + "; engineering disagreement threshold, not calibrated accuracy"}

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
            warnings.append("Прогноз без воздействия сигнализирует о риске превышения 10 мг/кг: удержание режима недопустимо; "
                            "корректирующий вариант оценивается по наблюдательным коэффициентам и требует подтверждения ЛИМС")

        other_quality = {}
        for name, supplied, metric_id in (
            ("t95", request.current_t95, "lims.ht.2.95%.T"),
            ("cetane", request.current_cetane, CETANE_METRIC),
        ):
            other_quality[name] = (
                {"source": f"request.current_{name}", "value": supplied}
                if supplied is not None
                else {"source": metric_id, **_evidence(values.get(metric_id))}
            )
        cetane = other_quality["cetane"]
        if cetane["source"].startswith("request."):
            cetane.update(lower_bound=cetane["value"], age_days=0.0, usable=True, basis="request value, no drift allowance")
        elif cetane.get("value") is not None and cetane.get("timestamp"):
            # Monthly lab property: keep the sample, widen it by the drift seen in earlier samples.
            sigma, pairs, source = drift_sigma(context.directory, parse_time(request.at))
            age = (parse_time(request.at) - parse_time(cetane["timestamp"])).total_seconds() / 86400
            cetane.update(aged_lower_bound(cetane["value"], age, sigma), drift_pairs=pairs, drift_source=source)
            if cetane["freshness"] != "fresh" and cetane["usable"]:
                warnings.append(f"Цетановое число ЛИМС измерено {age:.0f} сут назад; используется нижняя граница "
                                f"{cetane['lower_bound']:.1f} с учётом дрейфа — запросите свежий анализ")
        comparison = self._analyser_comparison(values, model_forecast)
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
                details = [FORECAST_REASONS.get(code, code) for code in (forecast or {}).get("reasons") or []]
                reasons.append("Нет допустимого модельного прогноза для решения по наблюдаемым данным"
                               + (": " + "; ".join(dict.fromkeys(details)) if details else ""))
            elif not forecast.get("path_supported", True):
                reasons.append("Не все горизонты траектории прошли проверку области применимости")
            # A forecast alarm is not a data failure: it makes holding the regime
            # inadmissible and is resolved per candidate by the sulphur risk gate.
            if forecast and forecast.get("horizon_minutes", forecast.get("forecast_horizon_minutes", 180)) != context.request.horizon_minutes:
                reasons.append("Горизонт сценария не совпадает с горизонтом независимой модели; прогноз не подтверждает эту конечную точку")
        analyzers = quality.get("evidence", {}).get("analyzer_comparison", {})
        if not explicit and analyzers.get("conflict"):
            reasons.append("Свежие поточные анализаторы серы расходятся; требуется проверка источников")
        control_values = {key: item.get("value") for key, item in quality["evidence"]["controls"].items()}
        state = operating_state(control_values)
        if state["state"] in {"shutdown", "transition"}:
            # Put the regime first: it explains every downstream data failure.
            reasons.insert(0, state["reason"])
        can_recommend = not reasons
        severity = regime_severity(control_values)
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
            "operating_state": state,
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
        reliability: dict[str, Any], quality: dict[str, Any], economic: bool = False,
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

        changes = {field: abs(scenario["controls"][metric]["change"] or 0.0) for field, metric in
                   (("temperature", "ht.T6"), ("feed_rate_pct", "ht.F9"), ("pressure", "ht.P13"))}
        check("step_within_model_limit", all(changes[k] <= STEP_LIMITS[k] + 1e-9 for k in STEP_LIMITS),
              f"Изменение за шаг больше модельного предела (T6 ±{STEP_LIMITS['temperature']:g} °C, "
              f"F9 ±{STEP_LIMITS['feed_rate_pct']:g}%, P13 ±{STEP_LIMITS['pressure']:g} МПа по истории 2023–2025)",
              "q99_of_60min_changes_not_plant_limit")
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
            risk = scenario.get("risk") or {}
            probability = risk.get("exceedance_probability")
            check("sulfur_risk_margin", risk.get("passed"),
                  (f"Риск превышения 10 мг/кг выше допустимого: P(S > 10) = {probability:.0%} > "
                   f"{risk['max_exceedance_probability']:.0%}" if probability is not None else
                   f"Сера {scenario['product_sulfur']:.2f} мг/кг не оставляет запаса "
                   f"{risk.get('margin_mgkg') or 0:g} мг/кг до предела 10"),
                  risk.get("basis") or "not_assessed")
            if economic:
                # Cost saving may not buy its gain with the upper half of the risk budget.
                check("economic_risk_budget", risk.get("within_design"),
                      (f"Экономия допустима только при P(S > 10) ≤ {risk['design_max_exceedance_probability']:.0%}; "
                       f"у варианта {probability:.0%}" if probability is not None else
                       f"Экономия допустима только при сере ≤ {risk.get('design_level', 0):.2f} мг/кг"),
                      "half_of_risk_budget_for_cost_saving")
            forecast = quality.get("evidence", {}).get("model_forecast") or {}
            if (reliability.get("basis") != "scenario_only" and forecast.get("alarm_above_10")
                    and risk.get("basis") != "calibrated_residual_quantiles"):
                check("forecast_alarm_resolved", False,
                      "Независимый прогноз указывает превышение 10 мг/кг, а риск варианта нельзя оценить по калиброванной модели",
                      "forecast_alarm_without_residual_quantiles")
            # The hydro-treatment surrogate has no validated T95/cetane effect:
            # T95 is set by the AVT cut, cetane only by the dosed improver.
            other = quality.get("evidence", {}).get("other_quality", {})
            for name in ("t95", "cetane"):
                item = other.get(name, {})
                value = item.get("value")
                if value is None:
                    checks.append({"name": name, "passed": None, "basis": "not_assessed_missing_quality_evidence"})
                    if reliability.get("basis") != "scenario_only":
                        reasons.append(f"Нет обязательного показателя качества {name}; допустимость наблюдаемого режима не подтверждена")
                    continue
                if not item.get("source", "").startswith("request."):
                    # A monthly cetane sample is judged by its drift bound, not by 48 h freshness.
                    failures = _evidence_failures(name, item, request.at, require_fresh=name == "t95")
                    if not item.get("usable", True):
                        failures.append(f"{name}: проба старше {item['max_age_days']:.0f} сут; нужен свежий анализ ЛИМС")
                    reasons.extend(failures)
                    checks.append({"name": f"{name}_evidence", "passed": not failures, "basis": "observed_baseline"})
                if name == "t95":
                    check("t95", value <= min(HARD_T95_MAX, request.targets.t95_max),
                          "Базовый показатель t95 не выполняет ограничение; режим гидроочистки T95 не меняет — "
                          "нужна смесь с лёгким компонентом или изменение отбора на АВТ", "editable_target_baseline_only")
                    continue
                need = required_additive(request, quality)
                product = item.get("lower_bound", value) + request.parameters.cetane_gain_per_pct * request.additive_pct
                check("cetane", product >= max(HARD_CETANE_MIN, request.targets.cetane_min) - 1e-9,
                      (f"Цетановое число: нижняя граница {item.get('lower_bound', value):.1f}; для ≥ {need['minimum']:g} "
                       f"нужно {need['pct']:.2f}% присадки — больше допустимых {MAX_ADDITIVE_PCT:g}%")
                      if need and not need["feasible"] else
                      "Цетановое число ниже ограничения при заданной дозе присадки",
                      "lab_lower_bound_plus_additive")
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

    # Exhaustive search box = the per-step model limit (1365 moves, ~0.1 s).
    SEARCH_GRID = {"temperature": tuple(float(v) for v in range(-6, 7)),
                   "feed_rate_pct": tuple(float(v) for v in range(-10, 11)),
                   "pressure": (-0.2, -0.1, 0.0, 0.1, 0.2)}

    @staticmethod
    def _is_corrective(changes: ControlChanges) -> bool:
        """Every control moves towards lower sulphur (or stays): the move buys margin, never spends it."""
        return changes.temperature >= 0 and changes.feed_rate_pct <= 0 and changes.pressure >= 0

    def _grid_optima(self, context: AgentContext, forecast: dict[str, Any] | None) -> dict[str, ControlChanges]:
        """Cheapest admissible cost-saving and corrective moves by the ranking loss.

        Any move that is not purely corrective spends sulphur margin (less heat
        or pressure, more feed — possibly compensated by more heat) and must end
        in the middle of the admissible risk zone; a corrective move only has
        to reach the zone, the loss already prices the remaining risk.  The
        winners still pass the full gate as ordinary candidates afterwards.
        """
        request = context.request
        limit = min(request.targets.sulfur_max, HARD_SULFUR_MAX)
        best: dict[str, tuple] = {}
        for temperature in self.SEARCH_GRID["temperature"]:
            for feed in self.SEARCH_GRID["feed_rate_pct"]:
                for pressure in self.SEARCH_GRID["pressure"]:
                    changes = ControlChanges(temperature=temperature, feed_rate_pct=feed, pressure=pressure)
                    if changes == ControlChanges():
                        continue
                    try:
                        scenario = calculate_scenario(context.directory, request.model_copy(update={"changes": changes}),
                                                      frame=context.frame, forecast=forecast)
                    except ValueError:
                        return {}
                    effort = self._effort(changes)
                    objectives = candidate_objectives(scenario, effort)
                    kind = "corrective_optimum" if self._is_corrective(changes) else "economic_optimum"
                    risk = scenario["risk"]
                    if (objectives["ranking_loss"] is None or not risk.get("passed")
                            or (kind == "economic_optimum" and not risk.get("within_design"))
                            or scenario["product_sulfur"] > limit
                            or not objectives["regime_severity"].get("within_model_limit")):
                        continue
                    key = (objectives["ranking_loss"], effort)
                    if kind not in best or key < best[kind][0]:
                        best[kind] = (key, changes)
        return {kind: item[1] for kind, item in best.items()}

    def _candidates(
        self, context: AgentContext, reliability: dict[str, Any], quality: dict[str, Any],
    ) -> list[dict[str, Any]]:
        request = context.request
        dose = required_additive(request, quality)
        if dose and dose["feasible"] and dose["pct"] > request.additive_pct:
            # Cetane ≥ 51 is a hard product limit: every direct-route candidate
            # carries the improver dose the aged lab bound requires.
            request = request.model_copy(update={"additive_pct": dose["pct"]}, deep=True)
            context = AgentContext(context.directory, request, context.frame)
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
        forecast = quality.get("evidence", {}).get("model_forecast")
        if not request.tanks and reliability.get("can_recommend"):
            optima = self._grid_optima(context, forecast)
            labels = {"economic_optimum": "Экономичный вариант: наименьшие потери при P(S > 10) в середине зоны риска",
                      "corrective_optimum": "Самое дешёвое изменение в сторону снижения серы"}
            for candidate_id in ("economic_optimum", "corrective_optimum"):
                optimum = optima.get(candidate_id)
                if candidate_id == "economic_optimum" and not request.optimize_economics:
                    continue
                if optimum is not None and all(optimum != changes for _, _, changes in definitions):
                    definitions.append((candidate_id, labels[candidate_id], optimum))
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
            gate = self._gate(scenario, candidate_context, reliability, quality,
                              economic=candidate_id.split("_mix")[0].split("_dose")[0] in ECONOMIC_CANDIDATES)
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
                    "risk": scenario.get("risk"),
                    "additive": scenario.get("additive"),
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
        annotate_pareto(candidates)
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
            # Name the blocking checks: the operator must see why no option exists.
            counts: dict[str, int] = {}
            for item in candidates:
                for reason in dict.fromkeys(item.get("safety_gate", {}).get("reasons", [])):
                    if reason not in reliability.get("reasons", []):
                        counts[reason] = counts.get(reason, 0) + 1
            blocking = sorted(counts, key=lambda r: -counts[r])[:3]
            evaluated = [c for c in candidates if c.get("status") == "ok" and c.get("product_sulfur") is not None]
            best = min(evaluated, key=lambda c: c["product_sulfur"]) if evaluated else None
            if best is not None and not reliability.get("reasons"):
                probability = (best.get("risk") or {}).get("exceedance_probability")
                blocking.append(f"лучший из вариантов ({best['id']}) даёт {best['product_sulfur']:.2f} мг/кг"
                                + (f", P(S > 10) = {probability:.0%}" if probability is not None else "")
                                + " в пределах модельного шага")
            return {
                "role": self.role,
                "status": "insufficient",
                "summary": "Ни один кандидат не прошёл все применимые проверки"
                           + (": " + "; ".join(blocking) if blocking else "; требуется ручной разбор"),
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
                "additive": result.get("additive"),
                "risk": result.get("risk"),
                "candidate_id": selected["id"],
                "objectives": selected["objectives"],
                "model_forecast": model_forecast,
            },
        }


ECONOMIC_CANDIDATES = ("lower_heat", "more_feed", "lower_pressure", "economic_optimum")
QUALITY_CHECKS = {"sulfur_hard_limit", "sulfur_editable_target", "blend_sulfur_hard_limit", "sulfur_risk_margin",
                  "economic_risk_budget"}
RELIABILITY_CHECKS = {"regime_severity_model_limit"}


class Orchestrator:
    """Runs the roles in order, resolves their conflicts, checks consistency and assembles the answer."""

    def __init__(self):
        self.quality = QualityAgent()
        self.reliability = ReliabilityAgent()
        self.optimization = OptimizationAgent()

    @staticmethod
    def _conflicts(
        request: ScenarioRequest, quality: dict[str, Any], reliability: dict[str, Any], optimization: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Explicit goal conflicts between the roles and how each one was resolved.

        Quality and hard constraints take priority over economics; a reliability
        limit takes priority over reaching the quality target by a change.
        """

        conflicts = []
        candidates = optimization.get("candidates") or []
        selected = optimization.get("selected_candidate")
        feasible = [c for c in candidates if c.get("feasible")]
        analyzers = quality.get("evidence", {}).get("analyzer_comparison") or {}
        if analyzers.get("conflict") and reliability.get("basis") != "scenario_only":
            conflicts.append({
                "code": "analyser_disagreement", "roles": ["quality", "reliability"], "resolution": "abstain",
                "message": f"Поточные анализаторы серы расходятся на {analyzers.get('difference_mgkg', 0):.1f} мг/кг; "
                           "источник базы не подтверждён",
            })

        def failed(candidate: dict[str, Any]) -> set[str]:
            return {c["name"] for c in candidate.get("safety_gate", {}).get("checks", []) if c.get("passed") is False}

        blocked = [c for c in candidates if c.get("status") == "ok" and not c.get("feasible") and c["id"] != "hold"
                   and c.get("target_met") and failed(c) and failed(c) <= RELIABILITY_CHECKS]
        if blocked:
            ids = ", ".join(c["id"] for c in blocked[:5])
            reachable = [c for c in feasible if c.get("target_met")]
            conflicts.append({
                "code": "quality_vs_reliability", "roles": ["quality", "reliability"],
                "resolution": "resolved_by_alternative" if reachable else "abstain",
                "message": (f"Цель по качеству достигается вариантами {ids}, но они превышают предел нагрузки режима"
                            + (f"; выбран допустимый вариант {selected}" if reachable and selected else
                               "; допустимого варианта, достигающего цели, нет")),
            })
        if request.optimize_economics:
            rejected = [c for c in candidates if c["id"].split("_mix")[0].split("_dose")[0] in ECONOMIC_CANDIDATES
                        and c.get("status") == "ok" and not c.get("feasible")
                        and failed(c) & QUALITY_CHECKS]
            if rejected:
                conflicts.append({
                    "code": "economy_vs_quality", "roles": ["optimization", "quality"], "resolution": "quality_priority",
                    "message": "Экономичные варианты отклонены из-за ограничения по сере: "
                               + ", ".join(c["id"] for c in rejected[:5]),
                })
        hold = next((c for c in candidates if c["id"] == "hold" and c.get("status") == "ok"), None)
        if (hold is not None and not hold.get("feasible") and failed(hold) & QUALITY_CHECKS
                and reliability.get("can_recommend")):
            risk = hold.get("risk") or {}
            probability = risk.get("exceedance_probability")
            state = (f"P(S > 10) = {probability:.0%} при допустимых {risk['max_exceedance_probability']:.0%}"
                     if probability is not None else f"сера {hold.get('product_sulfur', 0):.2f} мг/кг без запаса до 10")
            corrective = selected is not None and selected != "hold"
            conflicts.append({
                "code": "stability_vs_quality", "roles": ["quality", "optimization"],
                "resolution": "corrective_action" if corrective else "abstain",
                "message": (f"Удержание режима недопустимо: {state}; "
                            + (f"выбрано корректирующее изменение {selected}" if corrective else
                               "ни одно изменение T6/F9/P13 в пределах модели не возвращает риск в допустимую зону")),
            })
        if selected == "hold":
            hold = next((c for c in feasible if c["id"] == "hold"), None)
            better = [c for c in feasible if c["id"] != "hold" and hold is not None
                      and c["objectives"]["ranking_loss"] < hold["objectives"]["ranking_loss"]]
            if better:
                conflicts.append({
                    "code": "stability_vs_economy", "roles": ["optimization", "orchestrator"], "resolution": "hold",
                    "message": f"Вариант {better[0]['id']} немного выгоднее, но выигрыш меньше минимального "
                               f"({request.minimum_economic_gain:g}); режим удерживается",
                })
        return conflicts

    @staticmethod
    def _consistency(
        request: ScenarioRequest, quality: dict[str, Any], optimization: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Cross-role checks: the answer must be internally coherent before it is shown."""

        checks = []

        def check(code: str, passed: bool, message: str) -> None:
            checks.append({"code": code, "passed": bool(passed), "message": message})

        candidates = optimization.get("candidates") or []
        forecast = quality.get("evidence", {}).get("model_forecast") or {}
        at = parse_time(request.at)
        same_origin = all(parse_time(c["scenario"]["at"]) == at for c in candidates if c.get("scenario"))
        if forecast.get("feature_time"):
            same_origin = same_origin and parse_time(forecast["feature_time"]) <= at
        check("same_origin", same_origin,
              "Все кандидаты рассчитаны на момент решения, признаки прогноза не позже этого момента")
        if forecast:
            check("forecast_leakage", (forecast.get("leakage_check") or {}).get("passed", True),
                  "Прогноз прошёл проверку отсутствия будущих данных")
        selected_id = optimization.get("selected_candidate")
        selected = next((c for c in candidates if c["id"] == selected_id), None)
        if selected_id is None:
            return checks
        recommendation = optimization.get("recommendation") or {}
        check("selected_is_admissible", selected is not None and selected.get("feasible")
              and selected.get("safety_gate", {}).get("passed"),
              "Выбранный вариант прошёл все проверки")
        check("recommendation_matches_selected", selected is not None
              and recommendation.get("candidate_id") == selected_id
              and recommendation.get("controls") == selected.get("controls"),
              "Рекомендация совпадает с выбранным вариантом")
        if selected is not None:
            scenario = selected.get("scenario") or {}
            blend = scenario.get("blend") or {}
            within = (selected.get("product_sulfur") is not None and selected["product_sulfur"] <= HARD_SULFUR_MAX
                      and (not blend or blend.get("sulfur", 0) <= HARD_SULFUR_MAX))
            check("selected_within_hard_limits", within, "Сера выбранного варианта и смеси не выше 10 мг/кг")
            tanks = (scenario.get("applied_recipe") or {}).get("tanks") or []
            if tanks:
                check("blend_shares_sum_100", abs(sum(t["share"] for t in tanks) - 100) < 1e-6,
                      "Сумма долей компонентов смеси равна 100%")
        return checks

    @staticmethod
    def _assemble(
        request: ScenarioRequest, quality: dict[str, Any], reliability: dict[str, Any],
        optimization: dict[str, Any], conflicts: list[dict[str, Any]], consistency: list[dict[str, Any]],
    ) -> dict[str, Any]:
        abstain_reason = reliability.get("abstain_reason")
        if optimization["status"] == "error":
            abstain_reason = optimization.get("error")
        elif optimization["status"] != "ok" and not abstain_reason:
            abstain_reason = optimization["summary"]
        conflict_abstain = [c["message"] for c in conflicts if c["resolution"] == "abstain"]
        failed_checks = [c["message"] for c in consistency if not c["passed"]]
        if not abstain_reason and conflict_abstain:
            abstain_reason = "Конфликт целей: " + "; ".join(conflict_abstain)
        if not abstain_reason and failed_checks:
            abstain_reason = "Внутренняя несогласованность агентов: " + "; ".join(failed_checks)
        abstained = bool(abstain_reason) or optimization["status"] != "ok"
        safety_gate = optimization.get("safety_gate", {"passed": False, "reasons": [abstain_reason], "checks": []})
        if abstained and safety_gate.get("passed"):
            # The optimiser found an admissible candidate but the orchestrator
            # refused it; the published gate must reflect the refusal.
            safety_gate = {**safety_gate, "passed": False, "orchestrator_override": True,
                           "reasons": [abstain_reason, *safety_gate.get("reasons", [])]}
        orchestrator_status = "abstain" if abstained else "ok"
        trace = [
            {"step": 1, "role": quality["role"], "status": quality["status"], "summary": quality["summary"],
             "consumes": ["snapshot", "forecast"], "produces": ["sulfur baseline", "forecast", "other quality", "controls evidence"]},
            {"step": 2, "role": reliability["role"], "status": reliability["status"], "summary": reliability["summary"],
             "consumes": ["quality.evidence", "forecast.applicability"], "produces": ["data gates", "regime severity", "abstain reason"]},
            {"step": 3, "role": optimization["role"], "status": optimization["status"], "summary": optimization["summary"],
             "consumes": ["quality.evidence", "reliability.gates", "scenario model"],
             "produces": ["candidates", "safety gate", "pareto annotation", "selection"]},
            {"step": 4, "role": "orchestrator", "status": orchestrator_status,
             "summary": f"Конфликтов: {len(conflicts)}, согласованность: "
                        f"{sum(c['passed'] for c in consistency)}/{len(consistency)}",
             "consumes": ["quality", "reliability", "optimization"],
             "produces": ["conflict resolution", "consistency checks", "recommendation | abstain", "explanation"]},
        ]
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
            "safety_gate": safety_gate,
            "forecast": quality.get("evidence", {}).get("model_forecast"),
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

    def decide(self, directory: Path, request: ScenarioRequest) -> dict[str, Any]:
        frame = snapshot(directory, request.at)
        context = AgentContext(directory=directory, request=request, frame=frame)
        quality = self.quality.run(context)
        reliability = self.reliability.run(context, quality)
        optimization = self.optimization.run(context, reliability, quality)
        conflicts = self._conflicts(request, quality, reliability, optimization)
        consistency = self._consistency(request, quality, optimization)
        decision = self._assemble(request, quality, reliability, optimization, conflicts, consistency)
        # The explanation is attached after the decision is final: the optional
        # LLM sees the finished answer and cannot influence it.
        decision["explanation"] = llm_explain(decision, build_explanation(decision))
        return decision


def make_decision(directory: Path, request: ScenarioRequest) -> dict[str, Any]:
    """Convenience function used by the API and by offline tests."""

    return Orchestrator().decide(directory, request)
