from __future__ import annotations

from typing import Any

from .common import (
    CRITICAL_TAGS,
    CONTROLLED_PARAMETERS,
    FRESH_LIMS_HOURS,
    FRESH_PAK_HOURS,
    AgentResult,
    clamp,
    latest_quality_value,
    value_at,
)
from .feature_store import flatline_score, robust_profile, robust_z, trend


def run(process_state: dict[str, Any]) -> dict[str, Any]:
    avt = process_state.get("avt_telemetry", {})
    unit = process_state.get("unit_242000_telemetry", {})
    latest_lims = process_state.get("latest_lims", {})
    latest_pak = process_state.get("latest_pak", {})

    missing_critical_tags = []
    for section, tags in CRITICAL_TAGS.items():
        source = process_state.get(section, {})
        for tag in tags:
            if source.get(tag) is None:
                missing_critical_tags.append(tag)

    sulfur_lims = latest_lims.get("Mg.Sulfur")
    sulfur_pak = latest_pak.get("24-2000:Mg.Sulfur")
    lims_ages = [
        float(item.get("age_hours", 999))
        for item in latest_lims.values()
        if item.get("age_hours") is not None
    ]
    pak_ages = [
        float(item.get("age_hours", 999))
        for item in latest_pak.values()
        if item.get("age_hours") is not None
    ]
    lims_age_hours = (
        float(sulfur_lims["age_hours"])
        if sulfur_lims and sulfur_lims.get("age_hours") is not None
        else (min(lims_ages) if lims_ages else None)
    )
    pak_age_hours = (
        float(sulfur_pak["age_hours"])
        if sulfur_pak and sulfur_pak.get("age_hours") is not None
        else (min(pak_ages) if pak_ages else None)
    )

    anomalies: list[str] = []
    forbidden_changes: list[dict[str, str]] = []
    statistical_checks: dict[str, Any] = {}

    def forbid_parameter(parameter: str, reason: str) -> None:
        if parameter not in CONTROLLED_PARAMETERS:
            return
        for direction in ("increase", "decrease"):
            forbidden_changes.append(
                {
                    "parameter": parameter,
                    "direction": direction,
                    "reason": reason,
                }
            )

    for tag in missing_critical_tags:
        forbid_parameter(tag, f"{tag}: критичный тег недоступен, управление запрещено")
    if lims_age_hours is None:
        anomalies.append("ЛИМС недоступен для выбранного среза")
    elif lims_age_hours > FRESH_LIMS_HOURS:
        anomalies.append(f"ЛИМС устарел: {lims_age_hours:.1f} ч")

    if pak_age_hours is None:
        anomalies.append("ПАК недоступен для выбранного среза")
    elif pak_age_hours > FRESH_PAK_HOURS:
        anomalies.append(f"ПАК устарел: {pak_age_hours:.1f} ч")

    if sulfur_lims and sulfur_pak:
        diff = abs(float(sulfur_lims["value"]) - float(sulfur_pak["value"]))
        if diff > 1.5 and float(sulfur_lims.get("age_hours", 999)) <= FRESH_LIMS_HOURS:
            anomalies.append(f"ЛИМС и ПАК по сере расходятся на {diff:.2f} mg/kg")

    timestamp = process_state.get("timestamp", "")
    for table, section, tags, time_col in [
        ("unit_242000_telemetry", "unit_242000_telemetry", CRITICAL_TAGS["unit_242000_telemetry"], "date"),
        ("avt_telemetry", "avt_telemetry", CRITICAL_TAGS["avt_telemetry"], "date"),
    ]:
        source = process_state.get(section, {})
        for tag in tags:
            value = value_at(source, tag)
            profile = robust_profile(table, tag)
            z_score = robust_z(table, tag, value)
            flatline = flatline_score(table, time_col, tag, timestamp)
            tag_trend = trend(table, time_col, tag, timestamp)
            statistical_checks[tag] = {
                "value": value,
                "median": profile["median"],
                "p05": profile["p05"],
                "p95": profile["p95"],
                "robust_z": z_score,
                "flatline": flatline,
                "trend": tag_trend,
            }
            if z_score is not None and abs(z_score) >= 3.5:
                anomalies.append(f"{tag}: robust z-score {z_score} за пределами нормы")
            if flatline["is_flatline"]:
                anomalies.append(f"{tag}: flatline за последние {flatline['run_length']} точек")
                forbid_parameter(tag, f"{tag}: flatline, управление по недостоверному тегу запрещено")
            if tag_trend["delta"] is not None:
                p05 = profile["p05"]
                p95 = profile["p95"]
                span = None if p05 is None or p95 is None else p95 - p05
                if span and abs(tag_trend["delta"]) > span * 0.65:
                    anomalies.append(f"{tag}: резкое изменение {tag_trend['delta']:.3f} в последнем окне")

    confidence = 1.0
    confidence -= 0.12 * len(missing_critical_tags)
    if lims_age_hours is None or lims_age_hours > FRESH_LIMS_HOURS:
        confidence -= 0.18
    if pak_age_hours is None or pak_age_hours > FRESH_PAK_HOURS:
        confidence -= 0.12
    if anomalies:
        confidence -= min(0.35, 0.045 * len(anomalies))
    confidence = clamp(confidence)

    blocking_reasons = []
    if "T5" in missing_critical_tags or "P13" in missing_critical_tags:
        blocking_reasons.append("нет критичных режимных тегов T5/P13")
    if not avt and not unit:
        blocking_reasons.append("нет телеметрии АВТ и 24-2000")

    can_continue = not blocking_reasons
    can_recommend = can_continue and confidence >= 0.45
    data_status = "healthy"
    if not can_continue:
        data_status = "blocked"
    elif confidence < 0.75 or anomalies or missing_critical_tags:
        data_status = "limited"

    sulfur_source = latest_quality_value(
        process_state, "Mg.Sulfur", "24-2000:Mg.Sulfur"
    )["source"]
    d15_source = latest_quality_value(process_state, "D15", "24-2000:D15")["source"]

    result = AgentResult(
        agent="dq_agent",
        title="DQ Agent",
        input_summary={
            "timestamp": process_state.get("timestamp"),
            "avt_tags": len(avt),
            "unit_242000_tags": len(unit),
            "lims_indicators": len(latest_lims),
            "pak_indicators": len(latest_pak),
            "critical_tags": CRITICAL_TAGS,
            "historical_checks": len(statistical_checks),
        },
        analysis_steps=[
            "Проверены критичные теги АВТ и 24-2000.",
            "Оценена свежесть ЛИМС и ПАК относительно timestamp.",
            "Проверено расхождение ЛИМС и ПАК по сере.",
            "Для критичных тегов рассчитаны исторические медианы, p05/p95 и robust z-score.",
            "Проверены flatline и резкие изменения в последнем историческом окне.",
            "Рассчитан confidence и флаги can_continue/can_recommend на основе всех проверок.",
        ],
        output={
            "data_status": data_status,
            "confidence": round(confidence, 3),
            "freshness": {
                "lims_age_hours": lims_age_hours,
                "pak_age_minutes": None if pak_age_hours is None else round(pak_age_hours * 60, 1),
            },
            "missing_critical_tags": missing_critical_tags,
            "anomalies": anomalies,
            "forbidden_changes": forbidden_changes,
            "statistical_checks": statistical_checks,
            "source_priority_used": {
                "sulfur": sulfur_source,
                "D15": d15_source,
            },
            "can_continue": can_continue,
            "can_recommend": can_recommend,
            "blocking_reasons": blocking_reasons,
        },
    )
    return result.to_dict()
