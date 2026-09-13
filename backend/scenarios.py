"""Transparent, editable decision-support scenarios for the hackathon prototype."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .analytics import parse_time, snapshot


class QualityTargets(BaseModel):
    sulfur_max: float = Field(default=10, gt=0)
    t95_max: float = Field(default=360, gt=0)
    cetane_min: float = Field(default=51, gt=0)


class ModelParameters(BaseModel):
    lag_minutes: int = Field(default=90, ge=0, le=180)
    feed_sulfur_transfer: float = Field(default=0.20, gt=0)
    temperature_effect: float = Field(default=-0.08, lt=0)
    feed_rate_effect: float = Field(default=0.05, gt=0)
    pressure_effect: float = Field(default=-0.15, lt=0)
    cetane_gain_per_pct: float = Field(default=4.0, ge=0)


class ControlChanges(BaseModel):
    temperature: float = Field(default=0, ge=-10, le=10)
    feed_rate_pct: float = Field(default=0, ge=-10, le=10)
    pressure: float = Field(default=0, ge=-2, le=2)


class BlendTank(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    share: float = Field(ge=0, le=100)
    sulfur: float = Field(ge=0)
    t95: float = Field(gt=0)
    cetane: float = Field(gt=0)
    cost_index: float = Field(default=1, gt=0)


class ScenarioRequest(BaseModel):
    at: str
    horizon_minutes: int = Field(default=180, ge=0, le=180)
    step_minutes: int = Field(default=30, ge=15, le=60)
    baseline_feed_sulfur: float = Field(default=15, ge=0)
    feed_sulfur: float = Field(default=15, ge=0)
    current_sulfur: float | None = Field(default=None, ge=0)
    current_t95: float | None = Field(default=None, gt=0)
    current_cetane: float | None = Field(default=None, gt=0)
    targets: QualityTargets = Field(default_factory=QualityTargets)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    changes: ControlChanges | None = None
    tanks: list[BlendTank] = Field(default_factory=list, max_length=8)
    additive_pct: float = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def valid_step(self):
        if self.horizon_minutes and self.step_minutes > self.horizon_minutes:
            raise ValueError("Шаг не может быть больше горизонта")
        return self


def _value(values: dict, metric_id: str) -> float | None:
    item = values.get(metric_id)
    return item.get("value") if item else None


def _automatic_changes(required_reduction: float, model: ModelParameters) -> ControlChanges:
    if required_reduction <= 0:
        return ControlChanges()
    temperature = min(10.0, required_reduction / abs(model.temperature_effect))
    remaining = max(0.0, required_reduction + model.temperature_effect * temperature)
    pressure = min(2.0, remaining / abs(model.pressure_effect))
    remaining = max(0.0, remaining + model.pressure_effect * pressure)
    feed_rate = -min(10.0, remaining / model.feed_rate_effect)
    return ControlChanges(temperature=temperature, pressure=pressure, feed_rate_pct=feed_rate)


def _blend(request: ScenarioRequest) -> dict | None:
    if not request.tanks:
        return None
    total = sum(tank.share for tank in request.tanks)
    if total <= 0:
        raise ValueError("Суммарная доля резервуаров должна быть больше нуля")
    base = {
        key: sum(getattr(tank, key) * tank.share for tank in request.tanks) / total
        for key in ("sulfur", "t95", "cetane", "cost_index")
    }
    additive_fraction = request.additive_pct / 100
    result = {
        "sulfur": base["sulfur"] * (1 - additive_fraction),
        "t95": base["t95"] * (1 - additive_fraction),
        "cetane": base["cetane"] + request.parameters.cetane_gain_per_pct * request.additive_pct,
        "cost_index": base["cost_index"] * (1 - additive_fraction) + 100 * additive_fraction,
        "normalized_shares": [
            {"name": tank.name, "share": tank.share / total * 100} for tank in request.tanks
        ],
    }
    result["meets_targets"] = {
        "sulfur": result["sulfur"] <= request.targets.sulfur_max,
        "t95": result["t95"] <= request.targets.t95_max,
        "cetane": result["cetane"] >= request.targets.cetane_min,
    }
    result["all_targets_met"] = all(result["meets_targets"].values())
    return result


def calculate_scenario(directory: Path, request: ScenarioRequest) -> dict:
    frame = snapshot(directory, request.at)
    values = {item["metric_id"]: item for item in frame["values"]}
    sulfur = request.current_sulfur
    if sulfur is None:
        sulfur = _value(values, "pak.ht.Mg.Sulfur")
    if sulfur is None:
        sulfur = _value(values, "lims.ht.2.Mg.Sulfur")
    t95 = request.current_t95 or _value(values, "lims.ht.2.95%.T")
    cetane = request.current_cetane or _value(values, "lims.ht.2.CetaneNumber")
    if sulfur is None:
        raise ValueError("Нет базового значения серы: задайте его в сценарии")

    feed_effect = request.parameters.feed_sulfur_transfer * (
        request.feed_sulfur - request.baseline_feed_sulfur
    )
    changes = request.changes
    if changes is None:
        changes = _automatic_changes(
            sulfur + feed_effect - request.targets.sulfur_max,
            request.parameters,
        )
    full_effect = (
        feed_effect
        + request.parameters.temperature_effect * changes.temperature
        + request.parameters.feed_rate_effect * changes.feed_rate_pct
        + request.parameters.pressure_effect * changes.pressure
    )
    final_sulfur = max(0.0, sulfur + full_effect)
    target_time = parse_time(request.at)
    minutes = list(range(0, request.horizon_minutes + 1, request.step_minutes))
    if request.horizon_minutes not in minutes:
        minutes.append(request.horizon_minutes)
    trajectory = []
    for minute in minutes:
        response = (
            1.0
            if request.parameters.lag_minutes == 0
            else min(1.0, minute / request.parameters.lag_minutes)
        )
        trajectory.append(
            {
                "minute": minute,
                "timestamp": (target_time + timedelta(minutes=minute)).isoformat(),
                "sulfur": max(0.0, sulfur + full_effect * response),
            }
        )

    controls = {}
    for metric_id, change, relative in (
        ("ht.P8", changes.temperature, False),
        ("ht.T11", changes.feed_rate_pct, True),
        ("ht.F19", changes.pressure, False),
    ):
        current = _value(values, metric_id)
        controls[metric_id] = {
            "current": current,
            "change": change,
            "recommended": (
                current * (1 + change / 100)
                if current is not None and relative
                else current + change
                if current is not None
                else None
            ),
            "relative": relative,
        }

    return {
        "at": request.at,
        "horizon_minutes": request.horizon_minutes,
        "step_minutes": request.step_minutes,
        "baseline": {"sulfur": sulfur, "t95": t95, "cetane": cetane},
        "controls": controls,
        "predicted_sulfur": final_sulfur,
        "sulfur_target_met": final_sulfur <= request.targets.sulfur_max,
        "trajectory": trajectory,
        "blend": _blend(request),
        "assumptions": [
            "Простая линейная сценарная модель, не промышленный оптимизатор.",
            "Коэффициенты чувствительности и пределы изменения являются редактируемыми модельными допущениями.",
            "Лаг отклика задаётся в диапазоне 0–180 минут; значения ЛИМС доступны через 4 часа после отбора пробы.",
            "Присадка стоит в 100 раз дороже ДТ; её влияние на цетановое число задаётся параметром эффективности.",
        ],
    }
