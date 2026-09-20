"""Transparent, editable decision-support scenarios for the hackathon prototype."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import parse_time, snapshot
from .forecast import ForecastUnavailable, forecast_sulfur


# Physical source class is independent of file type: Q21 is also an online analyser.
SULFUR_PRIORITY_IDS = (
    "lims.ht.2.Mg.Sulfur",
    "pak.ht.Mg.Sulfur",
    # Q21 is the mapped online sulphur analyser after hydro-treatment.  Q20
    # is an upstream analyser and must not silently become the product
    # quality baseline.
    "ht.Q21",
    "vak.ht.Mg.Sulfur",
)
HARD_SULFUR_MAX = 10.0
HARD_T95_MAX = 360.0
HARD_CETANE_MIN = 51.0


class FiniteModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class QualityTargets(FiniteModel):
    sulfur_max: float = Field(default=10, gt=0)
    t95_max: float = Field(default=360, gt=0)
    cetane_min: float = Field(default=51, gt=0)


class ModelParameters(FiniteModel):
    lag_minutes: int = Field(default=90, ge=0, le=180)
    feed_sulfur_transfer: float = Field(default=0.20, gt=0)
    temperature_effect: float = Field(default=-0.08, lt=0)
    feed_rate_effect: float = Field(default=0.05, gt=0)
    pressure_effect: float = Field(default=-0.15, lt=0)
    cetane_gain_per_pct: float = Field(default=4.0, ge=0)
    dead_time_minutes: int = Field(default=0, ge=0, le=180)
    additive_sulfur_mgkg: float | None = Field(default=None, ge=0)


class ControlChanges(FiniteModel):
    temperature: float = Field(default=0, ge=-10, le=10)
    feed_rate_pct: float = Field(default=0, ge=-10, le=10)
    pressure: float = Field(default=0, ge=-2, le=2)


class BlendTank(FiniteModel):
    name: str = Field(min_length=1, max_length=80)
    share: float = Field(ge=0, le=100)
    sulfur: float = Field(ge=0)
    t95: float = Field(gt=0)
    cetane: float = Field(gt=0)
    cost_index: float = Field(default=1, gt=0)
    kind: Literal["stored", "hydrotreated_batch"] = "stored"
    stock_t: float | None = Field(default=None, ge=0)


class ScenarioRequest(FiniteModel):
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
    additive_stock_t: float | None = Field(default=None, ge=0)
    batch_mass_t: float | None = Field(default=None, gt=0)
    production_rate_tph: float | None = Field(default=None, gt=0)
    transport_delay_minutes: int = Field(default=0, ge=0, le=1440)
    intermediate_sulfur_max: float | None = Field(default=None, gt=0)
    optimize_economics: bool = True
    optimize_recipe: bool = False
    minimum_economic_gain: float = Field(default=0.03, ge=0)

    @model_validator(mode="after")
    def valid_step(self):
        if self.horizon_minutes and self.step_minutes > self.horizon_minutes:
            raise ValueError("Шаг не может быть больше горизонта")
        linked = [tank for tank in self.tanks if tank.kind == "hydrotreated_batch"]
        if len(linked) > 1:
            raise ValueError("В этой модели допускается один резервуар приёма новой партии")
        if linked and (linked[0].stock_t is None or self.batch_mass_t is None or self.production_rate_tph is None):
            raise ValueError("Для новой партии задайте запас резервуара, массу смеси и выпуск в т/ч")
        return self


def _value(values: dict, metric_id: str) -> float | None:
    item = values.get(metric_id)
    return item.get("value") if item else None


def select_sulfur(values: dict) -> tuple[float | None, str | None]:
    """Select the best available sulphur baseline and identify its source.

    ``values`` is the metric-id keyed snapshot map.  Values marked invalid or
    conflicting are already represented as ``None`` by ``snapshot`` and are
    therefore skipped. A stale lab result still reaches the reliability gate.
    Q21 and the separate PAK series share the online-analyser source class:
    among these two, prefer usable, fresh and then most recent evidence.
    """

    lab = _value(values, SULFUR_PRIORITY_IDS[0])
    if lab is not None:
        return lab, SULFUR_PRIORITY_IDS[0]
    analyzers = [mid for mid in ("pak.ht.Mg.Sulfur", "ht.Q21") if _value(values, mid) is not None]
    if analyzers:
        def rank(mid):
            item = values[mid]
            valid = not set(item.get("flags") or []) & {"invalid", "conflict", "suspect", "flatline", "gap"}
            return valid, item.get("freshness") == "fresh", str(item.get("timestamp") or "")
        chosen = max(analyzers, key=rank)
        return _value(values, chosen), chosen
    for metric_id in ("vak.ht.Mg.Sulfur",):
        value = _value(values, metric_id)
        if value is not None:
            return value, metric_id
    return None, None


def _automatic_changes(required_reduction: float, model: ModelParameters) -> ControlChanges:
    if required_reduction <= 0:
        return ControlChanges()
    temperature = min(10.0, required_reduction / abs(model.temperature_effect))
    remaining = max(0.0, required_reduction + model.temperature_effect * temperature)
    pressure = min(2.0, remaining / abs(model.pressure_effect))
    remaining = max(0.0, remaining + model.pressure_effect * pressure)
    feed_rate = -min(10.0, remaining / model.feed_rate_effect)
    return ControlChanges(temperature=temperature, pressure=pressure, feed_rate_pct=feed_rate)


def _response_fraction(minute: int, lag_minutes: int, dead_time_minutes: int = 0) -> float:
    """Editable ramp assumption, with no intervention effect at the origin."""
    if minute <= dead_time_minutes:
        return 0.0
    return 1.0 if lag_minutes == 0 else min(1.0, (minute - dead_time_minutes) / lag_minutes)


def _blend(request: ScenarioRequest, *, produced_sulfur: float | None = None,
           produced_mass: float = 0) -> dict | None:
    if not request.tanks:
        return None
    total = sum(tank.share for tank in request.tanks)
    if total <= 0:
        raise ValueError("Суммарная доля резервуаров должна быть больше нуля")
    # A blend recipe is a hard constraint.  Do not silently renormalize an
    # invalid recipe: doing so can make the displayed input differ from the
    # simulated one and hides an unsafe request.  A tiny tolerance covers
    # decimal representation noise while still rejecting meaningful errors.
    if abs(total - 100.0) > 1e-6:
        raise ValueError(
            f"Суммарная доля резервуаров должна быть равна 100%, сейчас {total:g}%"
        )
    components = []
    for tank in request.tanks:
        available = tank.stock_t
        sulfur = tank.sulfur
        if tank.kind == "hydrotreated_batch":
            available = tank.stock_t + produced_mass
            if available > 0 and produced_mass > 0:
                sulfur = (tank.stock_t * tank.sulfur + produced_mass * produced_sulfur) / available
        required = (request.batch_mass_t * (1-request.additive_pct/100) * tank.share/100
                    if request.batch_mass_t is not None else None)
        stock_met = True if tank.share == 0 else available >= required - 1e-8 if available is not None and required is not None else None
        components.append({**tank.model_dump(), "sulfur": sulfur, "available_t": available,
                           "required_t": required, "stock_met": stock_met})
    base = {
        key: sum(tank[key] * tank["share"] for tank in components) / total
        for key in ("sulfur", "t95", "cetane", "cost_index")
    }
    additive_fraction = request.additive_pct / 100
    additive_required = request.batch_mass_t * additive_fraction if request.batch_mass_t is not None else None
    additive_stock_met = (True if additive_fraction == 0 else
                          request.additive_stock_t >= additive_required - 1e-8
                          if request.additive_stock_t is not None and additive_required is not None else None)
    stock_checks = [t["stock_met"] for t in components] + [additive_stock_met]
    result = {
        "sulfur": (base["sulfur"] * (1 - additive_fraction) + request.parameters.additive_sulfur_mgkg * additive_fraction
                   if request.parameters.additive_sulfur_mgkg is not None else base["sulfur"]),
        "t95": base["t95"],
        "cetane": base["cetane"] + request.parameters.cetane_gain_per_pct * request.additive_pct,
        "cost_index": base["cost_index"] * (1 - additive_fraction) + 100 * additive_fraction,
        "normalized_shares": [
            {"name": tank.name, "share": tank.share / total * 100} for tank in request.tanks
        ],
        "components": components,
        "stock_constraints_met": (False if any(v is False for v in stock_checks)
                                  else True if all(v is True for v in stock_checks) else None),
        "additive_inventory": {"available_t": request.additive_stock_t, "required_t": additive_required, "stock_met": additive_stock_met},
        "additive_sulfur_assessed": additive_fraction == 0 or request.parameters.additive_sulfur_mgkg is not None,
        "additive_dose_kg_t": request.additive_pct * 10,
        "mass_basis": "component mass shares before additive; additive fraction of final batch",
        "quality_basis": "S mass balance; T95 and cetane are scenario surrogates; no T95 benefit from additive",
    }
    result["meets_targets"] = {
        "sulfur": result["sulfur"] <= min(HARD_SULFUR_MAX, request.targets.sulfur_max),
        "t95": result["t95"] <= min(HARD_T95_MAX, request.targets.t95_max),
        "cetane": result["cetane"] >= max(HARD_CETANE_MIN, request.targets.cetane_min),
    }
    result["all_targets_met"] = all(result["meets_targets"].values())
    return result


def _baseline_knots(forecast: dict | None, sulfur: float, horizon: int) -> list[tuple[float, float]]:
    """Scenario interpolation is linear in concentration; exact requested endpoint is retained."""
    if (forecast and forecast.get("status") == "ok" and forecast.get("path_supported")
            and forecast.get("nowcast") and forecast.get("horizon_minutes") == horizon):
        rows = [(float(r["minutes"]), float(r["prediction"])) for r in forecast["horizons"]
                if r["minutes"] < horizon and r.get("prediction") is not None]
        return sorted([*rows, (float(horizon), float(forecast["prediction"]))])
    return [(0., sulfur), (float(horizon), sulfur)] if horizon else [(0., sulfur)]


def _at_knots(knots: list[tuple[float, float]], minute: float) -> float:
    if minute <= knots[0][0]:
        return knots[0][1]
    for (a, x), (b, y) in zip(knots, knots[1:]):
        if minute <= b:
            return x + (y-x) * (minute-a) / (b-a)
    return knots[-1][1]


def _batch_integral(knots: list[tuple[float, float]], effect: float, duration: float,
                    lag: float, dead_time: float) -> float:
    """Exact integral of clipped piecewise-linear concentration, including an instant step."""
    points = sorted({0., duration, *[t for t, _ in knots if 0 < t < duration],
                     min(duration, dead_time), min(duration, dead_time+lag)})
    def concentration(t):
        return _at_knots(knots, t) + effect * _response_fraction(t, lag, dead_time)
    total = 0.
    for a, b in zip(points, points[1:]):
        # Interior values avoid giving a jump at dead_time nonzero integration mass.
        u, v = concentration(a+(b-a)/4), concentration(a+3*(b-a)/4)
        left, right = 1.5*u-.5*v, 1.5*v-.5*u
        if left >= 0 and right >= 0:
            total += (b-a)*(left+right)/2
        elif max(left, right) > 0:
            total += (b-a)*max(left, right)**2/(2*abs(right-left))
    return total


def calculate_scenario(directory: Path, request: ScenarioRequest, *, frame: dict | None = None,
                       forecast: dict | None = None) -> dict:
    frame = snapshot(directory, request.at) if frame is None else frame
    values = {item["metric_id"]: item for item in frame["values"]}
    sulfur = request.current_sulfur
    if sulfur is None and forecast is None:
        try:
            forecast = forecast_sulfur(directory, request.at, horizon_minutes=request.horizon_minutes)
        except ForecastUnavailable:
            forecast = None
    forecast_ok = bool(forecast and forecast.get("status") == "ok" and forecast.get("path_supported")
                       and forecast.get("nowcast") and forecast.get("horizon_minutes") == request.horizon_minutes)
    sulfur_source = "request.current_sulfur" if sulfur is not None else None
    if sulfur is None and forecast_ok:
        sulfur, sulfur_source = float(forecast["nowcast"]["prediction"]), "model.nowcast"
    if sulfur is None:
        sulfur, sulfur_source = select_sulfur(values)
    t95 = request.current_t95 or _value(values, "lims.ht.2.95%.T")
    cetane = request.current_cetane or _value(values, "lims.ht.2.CetaneNumber")
    if sulfur is None:
        raise ValueError("Нет базового значения серы: задайте его в сценарии")
    knots = _baseline_knots(forecast if request.current_sulfur is None else None, sulfur, request.horizon_minutes)
    endpoint = _at_knots(knots, request.horizon_minutes)

    feed_effect = request.parameters.feed_sulfur_transfer * (
        request.feed_sulfur - request.baseline_feed_sulfur
    )
    changes = request.changes
    if changes is None:
        response = _response_fraction(request.horizon_minutes, request.parameters.lag_minutes, request.parameters.dead_time_minutes)
        # Solve for the requested horizon, not for an unreachable steady state.
        # An editable target cannot relax the confirmed product sulphur limit.
        effective_target = min(request.targets.sulfur_max, HARD_SULFUR_MAX)
        if request.tanks:
            effective_target = request.intermediate_sulfur_max if request.intermediate_sulfur_max is not None else endpoint + max(0, feed_effect)
        changes = _automatic_changes(
            (endpoint - effective_target) / response + feed_effect if response else 0,
            request.parameters,
        )
    full_effect = (
        feed_effect
        + request.parameters.temperature_effect * changes.temperature
        + request.parameters.feed_rate_effect * changes.feed_rate_pct
        + request.parameters.pressure_effect * changes.pressure
    )
    steady_state_sulfur = max(0.0, endpoint + full_effect)
    target_time = parse_time(request.at)
    minutes = list(range(0, request.horizon_minutes + 1, request.step_minutes))
    if request.horizon_minutes not in minutes:
        minutes.append(request.horizon_minutes)
    trajectory = []
    for minute in minutes:
        response = _response_fraction(minute, request.parameters.lag_minutes, request.parameters.dead_time_minutes)
        trajectory.append(
            {
                "minute": minute,
                "timestamp": (target_time + timedelta(minutes=minute)).isoformat(),
                "sulfur": max(0.0, _at_knots(knots, minute) + full_effect * response),
                "baseline_sulfur": _at_knots(knots, minute),
            }
        )

    controls = {}
    for metric_id, change, relative in (
        ("ht.T6", changes.temperature, False),
        ("ht.F9", changes.feed_rate_pct, True),
        ("ht.P13", changes.pressure, False),
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

    final_sulfur = trajectory[-1]["sulfur"]
    # Integrate the linear response analytically; changing display step must not change batch chemistry.
    arriving_duration = max(0, request.horizon_minutes - request.transport_delay_minutes)
    integral_s = _batch_integral(knots, full_effect, arriving_duration,
                                 request.parameters.lag_minutes, request.parameters.dead_time_minutes)
    produced_sulfur = integral_s / arriving_duration if arriving_duration else sulfur
    produced_mass = (request.production_rate_tph or 0) * (1+changes.feed_rate_pct/100) * arriving_duration/60
    blend = _blend(request, produced_sulfur=produced_sulfur, produced_mass=produced_mass)
    product_sulfur = blend["sulfur"] if blend else final_sulfur
    return {
        "at": request.at,
        "horizon_minutes": request.horizon_minutes,
        "step_minutes": request.step_minutes,
        "baseline": {"sulfur": sulfur, "t95": t95, "cetane": cetane, "sulfur_source": sulfur_source,
                     "kind": "model_forecast" if sulfur_source == "model.nowcast" else "flat_baseline",
                     "knots": [{"minute": t, "sulfur": s} for t, s in knots]},
        "forecast": forecast if request.current_sulfur is None else None,
        "controls": controls,
        "predicted_sulfur": final_sulfur,
        "steady_state_sulfur": steady_state_sulfur,
        "product_sulfur": product_sulfur,
        "product_route": "blend" if blend else "direct",
        "model_request": request.model_copy(update={"changes": changes}).model_dump(exclude_none=True),
        "sulfur_target_met": product_sulfur <= request.targets.sulfur_max,
        "hard_sulfur_limit_met": product_sulfur <= HARD_SULFUR_MAX,
        "intermediate_sulfur_limit_met": request.intermediate_sulfur_max is None or final_sulfur <= request.intermediate_sulfur_max,
        "intermediate_sulfur_max": request.intermediate_sulfur_max,
        "batch": {"arriving_minutes": arriving_duration, "produced_t": produced_mass,
                  "produced_sulfur": produced_sulfur, "withdrawal_at_horizon": True},
        "hard_sulfur_max": HARD_SULFUR_MAX,
        "prediction_scope": "horizon_endpoint_surrogate",
        "trajectory": trajectory,
        "blend": blend,
        "applied_recipe": {"tanks": [t.model_dump() for t in request.tanks], "additive_pct": request.additive_pct},
        "assumptions": [
            "Простая линейная сценарная модель, не промышленный оптимизатор.",
            "Для наблюдаемого режима база — калиброванный прогноз H0/H1/H2/H3; между опорными точками линейная интерполяция концентрации. Качество поступления интегрируется до горизонта минус доставка, независимо от шага графика.",
            "Коэффициенты чувствительности и пределы изменения являются редактируемыми модельными допущениями.",
            "Параметр lag задаёт время линейного нарастания эффекта (0–180 минут), а не подтверждённое время прохождения продукта; значения ЛИМС доступны через 4 часа после отбора пробы.",
            "predicted_sulfur соответствует концу выбранного горизонта; steady_state_sulfur — полному условному эффекту. Проверка конечной точки не гарантирует качество на всём переходе.",
            "T95 и цетановое число гидроочистки не прогнозируются: известные базовые значения служат отдельными ограничениями; неизвестные показатели не считаются прошедшими проверку.",
            "Присадка стоит в 100 раз дороже ДТ; её влияние на цетановое число задаётся параметром эффективности.",
            "Предел 10 мг/кг относится к товарному продукту. Отдельный предел гидроочистки задаётся явно.",
            "Новая партия полностью перемешивается с запасом перед отбором смеси в конце горизонта; непрерывная отгрузка не моделируется. T95 и цетан новой партии задаются пользователем.",
            "Выпуск т/ч задаётся отдельно: спорный объёмный расход F15 не используется для массового баланса. Массовый выпуск меняется пропорционально изменению подачи при неизменном условном выходе.",
            "Дозировка 0–3% — модельный диапазон (0–30 кг/т), не установленный технологический предел. Присадке не приписывается снижение T95.",
        ],
    }
