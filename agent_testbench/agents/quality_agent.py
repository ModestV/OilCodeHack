from __future__ import annotations

from typing import Any

from .common import (
    SULFUR_LIMIT_MG_KG,
    AgentResult,
    clamp,
    latest_quality_value,
    sulfur_risk_class,
    value_at,
)
from .feature_store import predict_linear, train_sulfur_model


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    unit = process_state.get("unit_242000_telemetry", {})
    dq = process_state.get("agent_results", {}).get("data_quality", {})

    sulfur = latest_quality_value(process_state, "Mg.Sulfur", "24-2000:Mg.Sulfur")
    d15 = latest_quality_value(process_state, "D15", "24-2000:D15")

    feature_values = {
        "T5": value_at(unit, "T5"),
        "T6": value_at(unit, "T6"),
        "P13": value_at(unit, "P13"),
        "F9": value_at(unit, "F9"),
        "F15": value_at(unit, "F15"),
        "F26": value_at(unit, "F26"),
    }

    current_sulfur = sulfur["value"]
    current_d15 = d15["value"]

    model = train_sulfur_model()
    model_sulfur = predict_linear(model, feature_values)

    source_weight = {
        "LIMS": 0.75,
        "PAK": 0.62,
        "LIMS_STALE": 0.45,
        "UNAVAILABLE": 0.0,
    }.get(sulfur["source"], 0.35)
    model_weight = 1.0 - source_weight

    blended_current = current_sulfur
    if current_sulfur is not None and model_sulfur is not None:
        blended_current = current_sulfur * source_weight + model_sulfur * model_weight
    elif model_sulfur is not None:
        blended_current = model_sulfur

    drivers = []
    sensitivities = model.get("coefficients", {})
    if sensitivities:
        top_drivers = sorted(
            sensitivities.items(), key=lambda item: abs(item[1]), reverse=True
        )[:3]
        drivers.extend(
            [f"{feature}: чувствительность {coef:.5f} mg/kg на единицу" for feature, coef in top_drivers]
        )

    source_gap = None
    if current_sulfur is not None and model_sulfur is not None:
        source_gap = current_sulfur - model_sulfur
        if abs(source_gap) > max(0.5, float(model.get("rmse") or 0.0) * 1.5):
            drivers.append(
                f"измерение и модель расходятся на {source_gap:.2f} mg/kg"
            )

    if sulfur["source"] == "PAK":
        drivers.append("Для серы выбран свежий ПАК")
    if sulfur["source"] == "LIMS":
        drivers.append("Для серы выбран свежий ЛИМС")
    if sulfur["source"].endswith("STALE"):
        drivers.append("ЛИМС устарел, повышена роль модели/ПАК")

    uncertainty_margin = float(model.get("rmse") or 0.35)
    if sulfur["source"].endswith("STALE"):
        uncertainty_margin += 0.15
    if dq.get("data_status") == "limited":
        uncertainty_margin += 0.1

    forecast_sulfur = None
    if blended_current is not None:
        forecast_sulfur = blended_current + 0.35 * uncertainty_margin

    forecast_d15 = current_d15
    if current_d15 is not None and feature_values["F26"] is not None:
        forecast_d15 = current_d15 + (feature_values["F26"] - 210.0) * 0.02

    confidence = float(dq.get("confidence", 0.65))
    if sulfur["source"] == "UNAVAILABLE":
        confidence -= 0.25
    if d15["source"] == "UNAVAILABLE":
        confidence -= 0.08
    if model.get("status", "").startswith("trained"):
        confidence += min(0.08, max(0.0, float(model.get("r2") or 0.0)) * 0.05)
        if float(model.get("rmse") or 999) > 1.0:
            confidence -= 0.12
    else:
        confidence -= 0.2
    confidence = clamp(confidence)

    hard_violations = []
    if forecast_sulfur is not None and forecast_sulfur > SULFUR_LIMIT_MG_KG:
        hard_violations.append("forecast_sulfur_mg_kg > 10")

    result = AgentResult(
        agent="quality_agent",
        title="Quality Agent",
        input_summary={
            "data_status": dq.get("data_status"),
            "sulfur_source": sulfur["source"],
            "d15_source": d15["source"],
            "features": feature_values,
            "model_status": model.get("status"),
            "model_sample_count": model.get("sample_count"),
        },
        analysis_steps=[
            "Выбран источник качества по приоритету ЛИМС -> ПАК -> ВАК -> КИП.",
            "На исторических данных обучена линейная регрессия sulfur по КИП 24-2000.",
            "Рассчитан модельный прогноз sulfur и разница между моделью и измерением.",
            "Измерение ЛИМС/ПАК смешано с моделью по весам доверия к источнику.",
            "Прогноз дополнен uncertainty margin на основе RMSE, свежести данных и DQ.",
            "Проверен жёсткий предел sulfur <= 10 mg/kg.",
            "Confidence унаследован от DQ и скорректирован по качеству модели.",
        ],
        output={
            "model": model,
            "current_quality": {
                "sulfur_mg_kg": None if current_sulfur is None else round(current_sulfur, 3),
                "d15_kg_m3": None if current_d15 is None else round(current_d15, 3),
            },
            "model_quality": {
                "sulfur_mg_kg": None if model_sulfur is None else round(model_sulfur, 3),
                "source_gap_mg_kg": None if source_gap is None else round(source_gap, 3),
                "blended_current_sulfur_mg_kg": None if blended_current is None else round(blended_current, 3),
                "source_weight": source_weight,
                "model_weight": round(model_weight, 3),
                "uncertainty_margin_mg_kg": round(uncertainty_margin, 3),
            },
            "forecast_quality": {
                "sulfur_mg_kg": None if forecast_sulfur is None else round(forecast_sulfur, 3),
                "d15_kg_m3": None if forecast_d15 is None else round(forecast_d15, 3),
            },
            "spec_risks": {
                "sulfur_limit_10_mg_kg": sulfur_risk_class(forecast_sulfur),
                "d15": "unknown" if forecast_d15 is None else "low",
            },
            "confidence": round(confidence, 3),
            "main_drivers": drivers or ["критичных драйверов качества не найдено"],
            "hard_violations": hard_violations,
        },
    )
    return result.to_dict()
