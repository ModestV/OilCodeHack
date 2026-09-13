from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


SOURCE_PRIORITY = "LIMS -> PAK -> VAK -> KIP"
SULFUR_LIMIT_MG_KG = 10.0
FRESH_LIMS_HOURS = 12.0
FRESH_PAK_HOURS = 2.0
MIN_RECOMMEND_CONFIDENCE = 0.5

CRITICAL_TAGS = {
    "unit_242000_telemetry": ["T5", "P13", "F26", "F9"],
    "avt_telemetry": ["F30"],
}

CONTROLLED_PARAMETERS = {
    "T5": {"unit": "C", "min": 350.0, "max": 370.0},
    "F26": {"unit": "m3/h", "min": 180.0, "max": 240.0},
    "F9": {"unit": "m3/h", "min": 140.0, "max": 195.0},
    "F30": {"unit": "t/h", "min": 80.0, "max": 115.0},
}


@dataclass
class AgentResult:
    agent: str
    title: str
    input_summary: dict[str, Any]
    analysis_steps: list[str]
    output: dict[str, Any]
    status: str = "completed"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "title": self.title,
            "status": self.status,
            "input_summary": self.input_summary,
            "analysis_steps": self.analysis_steps,
            "output": self.output,
            "warnings": self.warnings,
        }


def clone_state(process_state: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(process_state)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def round_float(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def value_at(mapping: dict[str, Any], key: str) -> float | None:
    value = mapping.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_quality_value(
    process_state: dict[str, Any],
    analyte: str,
    pak_tag: str | None = None,
) -> dict[str, Any]:
    latest_lims = process_state.get("latest_lims", {})
    latest_pak = process_state.get("latest_pak", {})

    lims = latest_lims.get(analyte)
    if lims and lims.get("value") is not None:
        if float(lims.get("age_hours", 999)) <= FRESH_LIMS_HOURS:
            return {
                "source": "LIMS",
                "value": float(lims["value"]),
                "unit": lims.get("unit", ""),
                "age_hours": float(lims.get("age_hours", 0)),
            }

    if pak_tag:
        pak = latest_pak.get(pak_tag)
        if pak and pak.get("value") is not None:
            if float(pak.get("age_hours", 999)) <= FRESH_PAK_HOURS:
                return {
                    "source": "PAK",
                    "value": float(pak["value"]),
                    "unit": pak.get("unit", ""),
                    "age_hours": float(pak.get("age_hours", 0)),
                }

    if lims and lims.get("value") is not None:
        return {
            "source": "LIMS_STALE",
            "value": float(lims["value"]),
            "unit": lims.get("unit", ""),
            "age_hours": float(lims.get("age_hours", 0)),
        }

    return {"source": "UNAVAILABLE", "value": None, "unit": "", "age_hours": None}


def sulfur_risk_class(sulfur_mg_kg: float | None) -> str:
    if sulfur_mg_kg is None:
        return "unknown"
    if sulfur_mg_kg > SULFUR_LIMIT_MG_KG:
        return "critical"
    if sulfur_mg_kg >= 9.5:
        return "high"
    if sulfur_mg_kg >= 8.5:
        return "medium"
    return "low"


def confidence_label(confidence: float) -> str:
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"
