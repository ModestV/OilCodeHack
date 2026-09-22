"""Deterministic operator explanation of a decision.

Blocks follow the case requirements: moment and state → problem/risk →
proposed action → expected effect → constraint check → data confidence →
why this candidate beats the admissible alternatives.  Plain f-strings over
the decision payload; no external model is called.  An optional LLM layer may
paraphrase this text, but this template is always computed and is the
fallback.
"""

from __future__ import annotations

from typing import Any

CONTROL_NAMES = {
    "ht.T6": ("T6, температура на входе Р-202", "°C", 1),
    "ht.F9": ("F9, массовый расход сырья", "%", 1),
    "ht.P13": ("P13, давление на входе Р-202", "МПа", 2),
}


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _action_lines(controls: dict[str, Any]) -> list[str]:
    lines = []
    for metric_id, (name, unit, digits) in CONTROL_NAMES.items():
        item = controls.get(metric_id) or {}
        change = item.get("change") or 0.0
        if not change:
            continue
        current, recommended = item.get("current"), item.get("recommended")
        if item.get("relative"):
            lines.append(f"{name}: {_fmt(current, digits)} → {_fmt(recommended, digits)} ({change:+.1f} {unit})")
        else:
            lines.append(f"{name}: {_fmt(current, digits)} → {_fmt(recommended, digits)} {unit} ({change:+.{digits}f} {unit})")
    return lines or ["Удержать текущий режим: изменений не требуется"]


def _state(decision: dict[str, Any]) -> str:
    evidence = decision["agents"]["quality"].get("evidence", {})
    sulfur = evidence.get("sulfur") or {}
    parts = [f"Момент: {decision['at']}."]
    if sulfur.get("value") is not None:
        parts.append(f"Базовая сера: {sulfur['value']:.1f} мг/кг (источник {sulfur.get('source')}).")
    for metric_id, (name, _unit, digits) in CONTROL_NAMES.items():
        item = (evidence.get("controls") or {}).get(metric_id) or {}
        if item.get("value") is not None:
            parts.append(f"{name.split(',')[0]} = {item['value']:.{digits}f} ({item.get('freshness')}).")
    return " ".join(parts)


def _problem(decision: dict[str, Any]) -> str:
    forecast = decision.get("forecast") or {}
    lines = []
    if forecast.get("status") == "ok" and forecast.get("prediction") is not None:
        lines.append(
            f"Прогноз без воздействия на {forecast.get('horizon_minutes', 0):.0f} мин: {forecast['prediction']:.1f} мг/кг "
            f"[{_fmt(forecast.get('prediction_lower'))}–{_fmt(forecast.get('prediction_upper'))}], "
            f"P(>10) = {_fmt((forecast.get('exceedance_probability') or 0) * 100, 0, '%')}"
        )
        if forecast.get("alarm_above_10"):
            lines.append("прогноз сигнализирует о риске превышения 10 мг/кг")
    elif forecast.get("status"):
        lines.append(f"Прогноз недоступен ({forecast.get('status')})")
    lines.extend(c["message"] for c in decision.get("conflicts") or [] if c.get("resolution") == "abstain")
    return "; ".join(lines) if lines else "Отклонений качества и режима не обнаружено"


def _confidence(decision: dict[str, Any]) -> str:
    reliability = decision["agents"].get("reliability") or {}
    if reliability.get("can_recommend"):
        return "Проверки свежести, флагов и области применимости пройдены"
    reasons = reliability.get("reasons") or []
    return "Проверки данных не пройдены: " + ("; ".join(reasons) if reasons else "причина не указана")


def build_explanation(decision: dict[str, Any]) -> dict[str, Any]:
    time_state = _state(decision)
    problem = _problem(decision)
    confidence = _confidence(decision)
    warnings = [c["message"] for c in decision.get("conflicts") or [] if c.get("resolution") == "warning"]
    if decision["status"] == "abstain":
        reason = (decision.get("abstain") or {}).get("reason") or "нет допустимого варианта"
        constraints = "; ".join((decision.get("safety_gate") or {}).get("reasons") or []) or reason
        text = (
            f"{time_state} Надёжной рекомендации нет: {reason}. Проблема: {problem}. "
            f"Проверенные ограничения: {constraints}. Данные: {confidence}. Требуется ручной разбор технологом."
        )
        return {
            "source": "template", "time_state": time_state, "problem": problem,
            "action": "Рекомендация не выдана", "expected_effect": None, "constraints_check": constraints,
            "confidence": confidence, "rationale": reason, "alternatives": [], "warnings": warnings, "text": text,
        }

    recommendation = decision["recommendation"]
    selected = recommendation.get("candidate_id")
    objectives = recommendation.get("objectives") or {}
    severity = objectives.get("regime_severity") or {}
    action_lines = _action_lines(recommendation.get("controls") or {})
    effect = (
        f"Сера в конце горизонта: {_fmt(recommendation.get('predicted_sulfur'))} мг/кг "
        f"(цель ≤ {_fmt(recommendation.get('target_sulfur'))}); выпуск ×{_fmt(objectives.get('throughput_index'), 3)}, "
        f"индекс энергозатрат {_fmt(objectives.get('energy_cost_index'), 3)}, "
        f"индекс нагрузки {_fmt(severity.get('index'), 2)} ({severity.get('class') or '—'})"
    )
    gate = decision.get("safety_gate") or {}
    passed = [c["name"] for c in gate.get("checks", []) if c.get("passed") is True]
    unassessed = gate.get("unassessed") or []
    constraints = f"Пройдено: {', '.join(passed) or '—'}" + (f"; не оценено: {', '.join(unassessed)}" if unassessed else "")
    alternatives = [c for c in decision.get("candidates") or [] if c.get("feasible") and c["id"] != selected]
    comparison = [
        f"{c['label']}: сера {_fmt(c.get('product_sulfur'))}, потери {_fmt((c.get('objectives') or {}).get('ranking_loss'), 3)}"
        for c in alternatives[:3]
    ]
    if selected == "hold":
        rationale = "Удержание допустимо; изменение режима не даёт достаточного выигрыша по взвешенной цели"
    else:
        rationale = (
            f"Выбран допустимый вариант с наименьшими взвешенными потерями ({_fmt(objectives.get('ranking_loss'), 3)}: "
            "выпуск, энергия, нагрузка, масштаб изменения)"
        )
    if comparison:
        rationale += f". Допустимые альтернативы — {'; '.join(comparison)}"
    text = (
        f"{time_state} Проблема: {problem}. Действие: {'; '.join(action_lines)}. Ожидаемый эффект: {effect}. "
        f"Ограничения: {constraints}. Данные: {confidence}. Обоснование: {rationale}."
        + (f" Предупреждение: {'; '.join(warnings)}." if warnings else "")
        + " Рекомендация требует проверки технологом перед изменением режима."
    )
    return {
        "source": "template", "time_state": time_state, "problem": problem, "action": action_lines,
        "expected_effect": effect, "constraints_check": constraints, "confidence": confidence,
        "rationale": rationale, "alternatives": [{"id": c["id"], "label": c["label"]} for c in alternatives],
        "warnings": warnings, "text": text,
    }
