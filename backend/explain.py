"""Deterministic operator explanation of a decision (organisers' Table 6 blocks).

Blocks: time & state → problem/risk → proposed action → expected effect →
constraint check → confidence → why this candidate beats the admissible
alternatives.  Plain f-strings on the decision payload; no external model.
"""
from __future__ import annotations

from typing import Any

CONTROL_NAMES = {"ht.T6": ("T6, температура на входе Р-202", "°C"), "ht.F9": ("F9, массовый расход сырья", "%"),
                 "ht.P13": ("P13, давление на входе Р-202", "МПа")}


def _fmt(value, digits=1, suffix=""):
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _action_lines(controls: dict[str, Any]) -> list[str]:
    lines = []
    for metric_id, (name, unit) in CONTROL_NAMES.items():
        item = controls.get(metric_id) or {}
        change = item.get("change") or 0.0
        if not change:
            continue
        current, recommended = item.get("current"), item.get("recommended")
        if item.get("relative"):
            lines.append(f"{name}: {_fmt(current, 1)} → {_fmt(recommended, 1)} т/ч ({change:+.1f} {unit}, лаг ≈{item.get('lag_minutes', '—')} мин)")
        else:
            lines.append(f"{name}: {_fmt(current, 2 if unit == 'МПа' else 1)} → {_fmt(recommended, 2 if unit == 'МПа' else 1)} {unit} "
                         f"({change:+.2f} {unit}, лаг ≈{item.get('lag_minutes', '—')} мин)")
    return lines or ["Удержать текущий режим: изменений не требуется"]


def build_explanation(decision: dict[str, Any]) -> dict[str, Any]:
    quality = decision["agents"]["quality"]
    evidence = quality.get("evidence", {})
    forecast = evidence.get("model_forecast") or {}
    sulfur = evidence.get("sulfur", {})
    controls = evidence.get("controls", {})
    confidence = decision.get("confidence") or {}
    risk = decision.get("risk") or {}

    state_parts = [f"Момент: {decision['at']}."]
    if sulfur.get("value") is not None:
        state_parts.append(f"Сера сейчас: {sulfur['value']:.1f} мг/кг (источник {sulfur.get('source')}).")
    last_lab = evidence.get("last_lab_sulfur") or {}
    if last_lab.get("value") is not None:
        state_parts.append(f"Последняя проба ЛИМС: {last_lab['value']:.1f} мг/кг, свежесть {last_lab.get('freshness')}.")
    for metric_id, (name, unit) in CONTROL_NAMES.items():
        item = controls.get(metric_id) or {}
        if item.get("value") is not None:
            state_parts.append(f"{name.split(',')[0]} = {item['value']:.2f} ({item.get('freshness')}).")
    analysers = forecast.get("analysers") or {}
    for name, item in analysers.items():
        if item.get("value") is not None:
            state_parts.append(f"Анализатор {name}: {item['value']:.1f} мг/кг, давность {_fmt(item.get('age_minutes'), 0)} мин.")
    time_state = " ".join(state_parts)

    problem = decision.get("problem") or {}
    problem_lines = list(problem.get("reasons") or [])
    if risk.get("status") == "ok" and risk.get("class") != "normal":
        problem_lines.append(f"Риск режима {risk['class']} (индекс {risk['index']:.2f}, доминирует {risk.get('dominant')})")
    if forecast.get("status") == "ok" and forecast.get("prediction") is not None:
        problem_lines.append(f"Прогноз без воздействия на {forecast.get('horizon_minutes', 0):.0f} мин: {forecast['prediction']:.1f} мг/кг "
                             f"[{_fmt(forecast.get('prediction_lower'))}–{_fmt(forecast.get('prediction_upper'))}], "
                             f"P(>10) = {_fmt((forecast.get('exceedance_probability') or 0) * 100, 0, '%')}")
    problem_text = "; ".join(problem_lines) if problem_lines else "Отклонений качества и режима не обнаружено"

    weak = [f"{f['note']} — {f['score']:.2f}" for f in confidence.get("factors", []) if f["score"] < 1.0]
    confidence_text = (f"{confidence.get('class', '—')} ({_fmt(confidence.get('score'), 2)})" +
                       (": " + "; ".join(weak) if weak else "; все факторы качества данных в норме")) if confidence else "не оценена"
    if decision["status"] == "abstain":
        reason = (decision.get("abstain") or {}).get("reason") or "нет допустимого варианта"
        constraints_text = "; ".join(decision.get("safety_gate", {}).get("reasons") or []) or reason
        text = (f"Надёжной рекомендации нет: {reason}. {problem_text}. Проверенные ограничения: {constraints_text}. "
                f"Уверенность в данных: {confidence_text}. Требуется ручной разбор технологом.")
        return {"time_state": time_state, "problem": problem_text, "action": "Рекомендация не выдана", "expected_effect": None,
                "constraints_check": constraints_text, "confidence": {**confidence, "text": confidence_text}, "rationale": reason,
                "alternatives": [], "text": text}

    recommendation = decision["recommendation"]
    selected = recommendation.get("candidate_id")
    action_lines = _action_lines(recommendation.get("controls") or {})
    objectives = recommendation.get("objectives") or {}
    effect = (f"Сера в конце горизонта: {recommendation['predicted_sulfur']:.1f} мг/кг "
              f"[{_fmt(recommendation.get('predicted_sulfur_lower'))}–{_fmt(recommendation.get('predicted_sulfur_upper'))}], "
              f"P(>10) = {_fmt((recommendation.get('exceedance_probability') or 0) * 100, 0, '%')}; "
              f"выпуск ×{_fmt(objectives.get('throughput_index'), 3)}, индекс энергозатрат {_fmt(objectives.get('energy_cost_index'), 3)}, "
              f"индекс риска режима {_fmt(objectives.get('risk_index'), 2)} ({objectives.get('risk_class') or '—'})")
    gate = decision.get("safety_gate") or {}
    passed = [c["name"] for c in gate.get("checks", []) if c.get("passed") is True]
    unassessed = gate.get("unassessed") or []
    constraints_text = f"Пройдено: {', '.join(passed) or '—'}" + (f"; не оценено: {', '.join(unassessed)}" if unassessed else "")
    alternatives = decision.get("alternatives") or []
    if selected == "hold":
        rationale = ("Стабильный период: текущая и прогнозная сера в пределах цели, поэтому лишних управляющих воздействий не предлагается"
                     if decision.get("selection_rule") == "stable_period_hold" else
                     "Удержание допустимо и имеет наименьшие взвешенные потери среди допустимых вариантов")
    else:
        comparison = []
        for alt in alternatives:
            comparison.append(f"{alt['label']}: сера {alt['predicted_sulfur']:.1f}, P(>10) {_fmt((alt.get('exceedance_probability') or 0) * 100, 0, '%')}, "
                              f"потери {_fmt((alt.get('objectives') or {}).get('ranking_loss'), 3)}")
        rationale = (f"Выбран вариант с наименьшими взвешенными потерями на фронте Парето (потери {_fmt(objectives.get('ranking_loss'), 3)}: "
                     f"выпуск, энергия, риск режима, масштаб изменения, P(>10)). "
                     + (f"Ближайшие допустимые альтернативы — {'; '.join(comparison)}." if comparison else "Других допустимых альтернатив на фронте нет."))
    warnings = [c["message"] for c in decision.get("conflicts") or [] if c.get("resolution") == "warning"]
    text = (f"{time_state} Проблема: {problem_text}. Действие: {'; '.join(action_lines)}. Ожидаемый эффект: {effect}. "
            f"Ограничения: {constraints_text}. Уверенность: {confidence_text}. Обоснование: {rationale}"
            + (f" Предупреждение: {'; '.join(warnings)}." if warnings else "")
            + " Рекомендация требует проверки технологом перед изменением режима.")
    return {"time_state": time_state, "problem": problem_text, "action": action_lines, "expected_effect": effect,
            "constraints_check": constraints_text, "confidence": {**confidence, "text": confidence_text}, "rationale": rationale,
            "alternatives": [{"id": a["id"], "label": a["label"]} for a in alternatives], "warnings": warnings, "text": text}
