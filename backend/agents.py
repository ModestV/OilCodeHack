"""Deterministic multi-agent decision support for the local prototype.

The agents in this module are deliberately small, transparent Python rules.  They
provide the same separation of concerns as a future LLM or service based setup,
while keeping every decision reproducible in the closed hackathon network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analytics import snapshot
from .scenarios import ScenarioRequest, calculate_scenario, select_sulfur
from .forecast import ForecastUnavailable, forecast_sulfur

CONTROL_IDS = ("ht.T6", "ht.F9", "ht.P13")


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

        # Keep the benchmark model as an auditable evidence source.  A missing
        # artifact must not hide the transparent scenario fallback, so this is
        # a warning/evidence field rather than a hard quality gate.
        model_forecast = None
        try:
            model_forecast = forecast_sulfur(context.directory, request.at)
        except ForecastUnavailable as exc:
            warnings.append(f"Прогноз серы недоступен: {exc}")

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
        freshness = sulfur.get("freshness")
        missing = list(quality.get("missing", []))
        warnings = list(quality.get("warnings", []))
        confidence = 0.85 if explicit else 0.75
        if explicit:
            # Request values are already available to the caller and do not
            # carry a telemetry freshness state.
            pass
        elif freshness == "stale":
            confidence -= 0.35
            warnings.append("Свежесть базового измерения ниже порога")
        elif freshness == "missing" or sulfur.get("value") is None:
            confidence = 0.0
        control_count = quality["evidence"]["available_control_count"]
        if control_count == 0:
            # A quality value alone cannot yield an actionable control
            # recommendation.  Keep the response explainable, but abstain.
            confidence = min(confidence, 0.4)
        else:
            stale_controls = sum(
                evidence.get("freshness") == "stale"
                and evidence.get("value") is not None
                for evidence in quality["evidence"]["controls"].values()
            )
            if stale_controls:
                confidence -= min(0.1 * stale_controls, 0.3)
                warnings.append(f"Управляющих тегов с истёкшей давностью: {stale_controls}")
        confidence = round(max(0.0, min(1.0, confidence)), 3)

        can_recommend = not missing and confidence >= 0.5
        reason = None
        if missing:
            reason = "Недостаточно данных: отсутствует базовое значение серы"
        elif not can_recommend:
            reason = "Недостаточно надёжные данные для рекомендации"
        return {
            "role": self.role,
            "status": "ok" if can_recommend else "insufficient",
            "summary": (
                "Временная доступность и свежесть прошли контроль"
                if can_recommend
                else reason
            ),
            "confidence": confidence,
            "can_recommend": can_recommend,
            "warnings": warnings,
            "abstain_reason": reason,
        }


class OptimizationAgent:
    """Runs the existing transparent scenario model after reliability gates."""

    role = "optimization"

    def run(
        self,
        context: AgentContext,
        reliability: dict[str, Any],
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not reliability.get("can_recommend"):
            return {
                "role": self.role,
                "status": "skipped",
                "summary": "Расчёт пропущен: reliability agent не разрешил рекомендацию",
                "scenario": None,
            }
        try:
            result = calculate_scenario(context.directory, context.request)
        except ValueError as exc:
            return {
                "role": self.role,
                "status": "error",
                "summary": "Сценарная модель вернула ошибку",
                "error": str(exc),
                "scenario": None,
            }
        predicted = result["predicted_sulfur"]
        target = result["sulfur_target_met"]
        model_forecast = (quality or {}).get("evidence", {}).get("model_forecast")
        return {
            "role": self.role,
            "status": "ok",
            "summary": (
                "Сценарий достигает цели по сере"
                if target
                else "Сценарий не достигает цели по сере; требуется ручная проверка"
            ),
            "scenario": result,
            "model_forecast": model_forecast,
            "recommendation": {
                "action": "apply_controls" if target else "escalate",
                "predicted_sulfur": predicted,
                "target_sulfur": context.request.targets.sulfur_max,
                "target_met": target,
                "controls": result["controls"],
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
        abstained = bool(abstain_reason) or optimization["status"] != "ok"
        return {
            "at": request.at,
            "status": "abstain" if abstained else "recommendation",
            "recommendation": None if abstained else optimization.get("recommendation"),
            "scenario": None if abstained else optimization.get("scenario"),
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
            ],
        }


def make_decision(directory: Path, request: ScenarioRequest) -> dict[str, Any]:
    """Convenience function used by the API and by offline tests."""

    return Orchestrator().decide(directory, request)
