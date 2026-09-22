"""Optional local LLM explainer over an OpenAI-compatible chat API.

The decision is always made by the deterministic agents.  When enabled, a local
model (Ollama, vLLM, llama.cpp server or the organisers' gateway) only rewrites
the template explanation for the operator.  Its answer is accepted only if

* every number in it can be traced to the decision facts (up to rounding);
* on ``abstain`` it gives no control advice and states the refusal;
* on a recommendation it names every changed control, or says "hold" for hold.

Otherwise, or on any transport error, the template explanation is returned
with ``fallback_reason``.  Disabled unless ``OILCODE_LLM_BASE_URL`` and
``OILCODE_LLM_MODEL`` are set; no dependency beyond the standard library.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

SYSTEM_PROMPT = (
    "Ты помощник оператора установки гидроочистки дизельного топлива. "
    "Тебе передают JSON с уже принятым решением детерминированной мультиагентной системы. "
    "Объясни решение оператору по-русски, 3–6 предложений, без markdown. "
    "Правила: не меняй решение; не добавляй чисел, которых нет в JSON; "
    "называй параметры управления их тегами T6, F9, P13; "
    "если status = abstain — прямо скажи, что рекомендация не выдана, назови причину и не предлагай изменений режима; "
    "если выбран hold — скажи, что режим удерживается; "
    "иначе назови каждый изменяемый параметр, ожидаемую серу и главную причину выбора."
)
TAGS = {"ht.T6": "T6", "ht.F9": "F9", "ht.P13": "P13"}
# Digits glued to a preceding letter are tags (T6, P13, T95), not quantities;
# digits followed by a unit ("7.9мг") are quantities and must be checked.
ANSWER_NUMBER = re.compile(r"(?<![\w.,])-?\d+(?:[.,]\d+)?")
# Names and hard specification limits the operator text may always mention:
# reactor Р-202, unit 24-2000, T95 ≤ 360 °C, cetane ≥ 51, sulphur ≤ 10 mg/kg.
DOMAIN_NUMBERS = {202.0, 24.0, 2000.0, 360.0, 51.0, 10.0}
FACT_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")
ADVICE = re.compile(
    r"(рекоменд\w*|предлага\w*|предлож\w*|следует|необходимо|нужно|стоит)\s+(?:\S+\s+){0,2}?"
    r"(увелич|повыс|сниз|подн|уменьш|измен)",
    re.IGNORECASE,
)
REFUSAL = re.compile(r"(не выда|нет надёжн|нет надежн|нет рекоменд|рекомендаци\w* нет|отказ|не рекомендует)", re.IGNORECASE)
HOLD = re.compile(r"(удерж|без изменени|текущ\w* режим|сохран\w* режим)", re.IGNORECASE)
MAX_CHARS = 2500


def settings() -> dict[str, Any]:
    base_url = (os.environ.get("OILCODE_LLM_BASE_URL") or "").rstrip("/")
    model = os.environ.get("OILCODE_LLM_MODEL") or None
    return {
        "enabled": bool(base_url and model),
        "base_url": base_url or None,
        "model": model,
        "timeout_s": float(os.environ.get("OILCODE_LLM_TIMEOUT_S", "30")),
        "max_tokens": int(os.environ.get("OILCODE_LLM_MAX_TOKENS", "800")),
        "api_key_set": bool(os.environ.get("OILCODE_LLM_API_KEY")),
    }


def facts(decision: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    """Compact, audit-friendly view of the decision; the only input the model sees."""

    forecast = decision.get("forecast") or {}
    recommendation = decision.get("recommendation") or {}
    probability = forecast.get("exceedance_probability")
    controls = {
        TAGS[key]: {k: item.get(k) for k in ("current", "change", "recommended")}
        for key, item in (recommendation.get("controls") or {}).items() if key in TAGS
    }
    objectives = recommendation.get("objectives") or {}
    return {
        "at": decision.get("at"),
        "status": decision.get("status"),
        "selected_candidate": decision.get("selected_candidate"),
        "sulfur_limit_mg_kg": 10,
        "forecast": {
            "horizon_minutes": forecast.get("horizon_minutes"),
            "horizon_hours": (forecast.get("horizon_minutes") or 0) / 60,
            "prediction_mg_kg": forecast.get("prediction"),
            "interval_mg_kg": [forecast.get("prediction_lower"), forecast.get("prediction_upper")],
            "exceedance_probability": probability,
            "exceedance_probability_pct": None if probability is None else round(probability * 100),
            "alarm_above_10": forecast.get("alarm_above_10"),
        } if forecast else None,
        "recommendation": {
            "controls": controls,
            "predicted_sulfur_mg_kg": recommendation.get("predicted_sulfur"),
            "target_sulfur_mg_kg": recommendation.get("target_sulfur"),
            "throughput_index": objectives.get("throughput_index"),
            "energy_cost_index": objectives.get("energy_cost_index"),
            "ranking_loss": objectives.get("ranking_loss"),
            "exceedance_probability": (recommendation.get("risk") or {}).get("exceedance_probability"),
            "additive_pct": (recommendation.get("additive") or {}).get("pct"),
            "additive_dose_kg_t": (recommendation.get("additive") or {}).get("dose_kg_t"),
            "product_cost_index": objectives.get("blend_cost_index"),
        } if recommendation else None,
        "abstain_reason": (decision.get("abstain") or {}).get("reason"),
        "failed_checks": (decision.get("safety_gate") or {}).get("reasons") or [],
        "conflicts": [{"code": c["code"], "resolution": c["resolution"], "message": c["message"]}
                      for c in decision.get("conflicts") or []],
        "alternatives": [
            {"id": c["id"], "label": c["label"], "product_sulfur_mg_kg": c.get("product_sulfur"),
             "ranking_loss": (c.get("objectives") or {}).get("ranking_loss")}
            for c in decision.get("candidates") or [] if c.get("feasible") and c["id"] != decision.get("selected_candidate")
        ][:3],
        "template_explanation": template.get("text"),
    }


def validate_numbers(text: str, grounded: Any, template_text: str) -> dict[str, Any]:
    source = json.dumps(grounded, ensure_ascii=False) + " " + (template_text or "")
    allowed = {float(item) for item in FACT_NUMBER.findall(source)} | DOMAIN_NUMBERS
    unknown = []
    for raw in ANSWER_NUMBER.findall(text):
        value = float(raw.replace(",", "."))
        decimals = len(raw.replace(",", ".").split(".")[1]) if re.search(r"[.,]", raw) else 0
        if not any(abs(round(candidate, decimals) - value) < 1e-9 or abs(round(abs(candidate), decimals) - abs(value)) < 1e-9
                   for candidate in allowed):
            unknown.append(raw)
    return {"passed": not unknown, "unknown_numbers": unknown}


def validate_semantics(text: str, decision: dict[str, Any]) -> dict[str, Any]:
    problems = []
    if decision.get("status") == "abstain":
        if ADVICE.search(text):
            problems.append("status=abstain: ответ содержит совет по изменению режима")
        if not REFUSAL.search(text):
            problems.append("status=abstain: ответ не сообщает, что рекомендация не выдана")
    else:
        controls = (decision.get("recommendation") or {}).get("controls") or {}
        changed = [TAGS[key] for key, item in controls.items() if key in TAGS and (item.get("change") or 0)]
        if not changed and not HOLD.search(text):
            problems.append("hold: ответ не говорит, что режим удерживается")
        missing = [tag for tag in changed if not re.search(rf"\b{tag}\b", text)]
        if missing:
            problems.append(f"не названы изменяемые параметры: {', '.join(missing)}")
    return {"passed": not problems, "problems": problems}


def _chat(config: dict[str, Any], messages: list[dict[str, str]]) -> str:
    body = json.dumps({"model": config["model"], "messages": messages, "temperature": 0,
                       "max_tokens": config["max_tokens"], "stream": False}).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("OILCODE_LLM_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"{config['base_url']}/chat/completions", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=config["timeout_s"]) as response:  # noqa: S310 - operator-configured URL
        payload = json.loads(response.read().decode("utf-8"))
    return (payload["choices"][0]["message"].get("content") or "").strip()


def explain(decision: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    config = settings()
    if not config["enabled"]:
        return template
    grounded = facts(decision, template)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(grounded, ensure_ascii=False)}]
    audit = {"model": config["model"], "base_url": config["base_url"], "messages": messages}
    started = time.perf_counter()
    try:
        text = _chat(config, messages)
    except (OSError, ValueError, KeyError, IndexError, TypeError, urllib.error.URLError) as exc:
        audit["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return {**template, "fallback_reason": f"LLM недоступна: {type(exc).__name__}: {exc}", "llm": audit}
    audit["latency_ms"] = round((time.perf_counter() - started) * 1000)
    audit["raw_answer"] = text
    numbers = validate_numbers(text, grounded, template.get("text", ""))
    semantics = validate_semantics(text, decision)
    problems = ([] if text else ["пустой ответ"]) + ([] if len(text) <= MAX_CHARS else ["слишком длинный ответ"])
    if not numbers["passed"]:
        problems.append(f"числа без основания в решении: {', '.join(numbers['unknown_numbers'])}")
    problems.extend(semantics["problems"])
    audit["validation"] = {"passed": not problems, "problems": problems, **numbers}
    if problems:
        return {**template, "fallback_reason": "; ".join(problems), "llm": audit}
    if "технолог" not in text:
        text += (" Требуется ручной разбор технологом." if decision.get("status") == "abstain"
                 else " Рекомендация требует проверки технологом перед изменением режима.")
    return {**template, "source": "llm", "text": text, "template_text": template.get("text"), "llm": audit}
