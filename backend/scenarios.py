"""Transparent, editable decision-support scenarios for the hackathon prototype.

The hydro-treatment surrogate is multiplicative (log-domain): every control
move scales product sulphur by ``exp(coefficient * change)`` with a linear
ramp over ``lag_minutes``.  The default coefficients are the median lagged
responses estimated from online-analyser step events
(``reports/modeling/sulfur-forecast/model.json`` → ``control_response``); the
expert confirmed only the direction for temperature, so the numbers stay
editable assumptions, not certified plant gains.
"""

from __future__ import annotations

from datetime import timedelta
from math import exp, log
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import parse_time, snapshot
from .forecast import ForecastUnavailable, _load_artifact, exceedance_at, forecast_sulfur


# The source order follows the case requirement: a laboratory result is the
# control fact, then the online analyser, then a future VAK result if one is
# registered, and only then the available KIP sulphur analysers.  The current
# bundle has no VAK metric for sulphur; ``vak.ht.Mg.Sulfur`` is kept as an
# explicit insertion slot so a future registered VAK value is considered
# before KIP without changing the source-priority contract.  It does not
# invent a derived value while the metric is absent.  Keep this list in one
# place so the scenario and agent paths cannot drift.
SULFUR_PRIORITY_IDS = (
    "lims.ht.2.Mg.Sulfur",
    "pak.ht.Mg.Sulfur",
    "vak.ht.Mg.Sulfur",
    # Q21 is the mapped online sulphur analyser after hydro-treating.  Q20
    # is an upstream analyser and must not silently become the product
    # quality baseline.
    "ht.Q21",
)
FEED_SULFUR_METRIC = "lims.ht.1.Mass.Sulfur"
HARD_SULFUR_MAX = 10.0
HARD_T95_MAX = 360.0
HARD_CETANE_MIN = 51.0
# Expert guidance: keep a 1-2 mg/kg technological margin below the limit.
DEFAULT_SULFUR_TARGET = 9.0
DEFAULT_MAX_EXCEEDANCE_PROBABILITY = 0.3
# Fallback defaults equal the rounded analyser step-response estimates in the
# shipped artifact; ``scenario_defaults()`` reads the live values.
FALLBACK_RESPONSE = {"temperature_effect": -0.0365, "feed_rate_effect": 0.0186, "pressure_effect": -0.185, "lag_minutes": 120}
DEFAULT_FEED_SULFUR_WT_PCT = 0.93
# Model bounds for a single recommendation step.  These are prototype
# assumptions (the organisers provide no rate-of-change or passport limits).
TEMPERATURE_CHANGE_LIMIT = 10.0
FEED_RATE_CHANGE_LIMIT_PCT = 10.0
PRESSURE_CHANGE_LIMIT = 0.5
# Numerical tolerance for target comparisons after solving to the target exactly.
TOLERANCE = 1e-9


class FiniteModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class QualityTargets(FiniteModel):
    sulfur_max: float = Field(default=DEFAULT_SULFUR_TARGET, gt=0)
    t95_max: float = Field(default=HARD_T95_MAX, gt=0)
    cetane_min: float = Field(default=HARD_CETANE_MIN, gt=0)
    max_exceedance_probability: float = Field(default=DEFAULT_MAX_EXCEEDANCE_PROBABILITY, ge=0, le=1)


class ModelParameters(FiniteModel):
    """Editable surrogate coefficients; ``None`` means "use the estimated default"."""

    lag_minutes: int | None = Field(default=None, ge=0, le=180)
    feed_sulfur_elasticity: float = Field(default=1.0, ge=0, le=2)
    temperature_effect: float | None = Field(default=None, lt=0)
    feed_rate_effect: float | None = Field(default=None, gt=0)
    pressure_effect: float | None = Field(default=None, lt=0)
    cetane_gain_per_pct: float = Field(default=4.0, ge=0)


class ControlChanges(FiniteModel):
    temperature: float = Field(default=0, ge=-TEMPERATURE_CHANGE_LIMIT, le=TEMPERATURE_CHANGE_LIMIT)
    feed_rate_pct: float = Field(default=0, ge=-FEED_RATE_CHANGE_LIMIT_PCT, le=FEED_RATE_CHANGE_LIMIT_PCT)
    pressure: float = Field(default=0, ge=-PRESSURE_CHANGE_LIMIT, le=PRESSURE_CHANGE_LIMIT)


class BlendTank(FiniteModel):
    name: str = Field(min_length=1, max_length=80)
    share: float = Field(ge=0, le=100)
    sulfur: float = Field(ge=0)
    t95: float = Field(gt=0)
    cetane: float = Field(gt=0)
    cost_index: float = Field(default=1, gt=0)


class ScenarioRequest(FiniteModel):
    at: str
    horizon_minutes: int = Field(default=180, ge=0, le=180)
    step_minutes: int = Field(default=30, ge=15, le=60)
    # Feed sulphur in % mass (LIMS hydro-treating point 1 ``Mass.Sulfur``).
    # ``None`` takes the last published laboratory value at ``at``.
    baseline_feed_sulfur: float | None = Field(default=None, gt=0)
    feed_sulfur: float | None = Field(default=None, gt=0)
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


def scenario_defaults() -> dict:
    """Effective default coefficients with their evidence."""
    try:
        artifact = _load_artifact()
        response = artifact.get("control_response") or {}
        if all(response.get(k, {}).get("coefficient") is not None for k in ("T6", "F9", "P13")):
            return {
                "temperature_effect": float(response["T6"]["coefficient"]),
                "feed_rate_effect": float(response["F9"]["coefficient"]),
                "pressure_effect": float(response["P13"]["coefficient"]),
                "lag_minutes": int(max(response[k].get("lag_minutes") or 60 for k in ("T6", "F9", "P13"))),
                "feed_sulfur_elasticity": 1.0,
                "units": {"temperature_effect": "ln(мг/кг) на °C", "feed_rate_effect": "ln(мг/кг) на % расхода",
                          "pressure_effect": "ln(мг/кг) на МПа", "feed_sulfur_elasticity": "ln S_out на ln S_in"},
                "events": {k: int(response[k].get("events", 0)) for k in ("T6", "F9", "P13")},
                "basis": "медианный лагированный отклик ln(серы) анализатора Q21 на ступени одного тега (наблюдательная оценка)",
                "source": "reports/modeling/sulfur-forecast/model.json", "artifact_sha256": artifact["sha256"],
            }
    except ForecastUnavailable:
        pass
    return {**FALLBACK_RESPONSE, "feed_sulfur_elasticity": 1.0, "basis": "встроенные умолчания (артефакт недоступен)", "source": None}


def resolve_parameters(parameters: ModelParameters) -> tuple[dict, dict]:
    defaults = scenario_defaults()
    resolved = {
        "lag_minutes": parameters.lag_minutes if parameters.lag_minutes is not None else defaults["lag_minutes"],
        "feed_sulfur_elasticity": parameters.feed_sulfur_elasticity,
        "temperature_effect": parameters.temperature_effect if parameters.temperature_effect is not None else defaults["temperature_effect"],
        "feed_rate_effect": parameters.feed_rate_effect if parameters.feed_rate_effect is not None else defaults["feed_rate_effect"],
        "pressure_effect": parameters.pressure_effect if parameters.pressure_effect is not None else defaults["pressure_effect"],
        "cetane_gain_per_pct": parameters.cetane_gain_per_pct,
    }
    return resolved, defaults


def _value(values: dict, metric_id: str) -> float | None:
    item = values.get(metric_id)
    return item.get("value") if item else None


def select_sulfur(values: dict) -> tuple[float | None, str | None]:
    """Select the best available raw sulphur baseline and identify its source.

    ``values`` is the metric-id keyed snapshot map.  Values marked invalid or
    conflicting are already represented as ``None`` by ``snapshot`` and are
    therefore skipped.  A stale higher-priority result is still selected and
    is subsequently handled by the reliability gate; silently replacing it
    with a lower-priority source would violate the source-priority contract.
    """

    for metric_id in SULFUR_PRIORITY_IDS:
        value = _value(values, metric_id)
        if value is not None:
            return value, metric_id
    return None, None


def _automatic_changes(required_ln_reduction: float, model: dict) -> ControlChanges:
    """Cheapest-first split of a required ln reduction: temperature, then pressure, then feed."""
    if required_ln_reduction <= 0:
        return ControlChanges()
    temperature = min(TEMPERATURE_CHANGE_LIMIT, required_ln_reduction / abs(model["temperature_effect"]))
    remaining = max(0.0, required_ln_reduction + model["temperature_effect"] * temperature)
    pressure = min(PRESSURE_CHANGE_LIMIT, remaining / abs(model["pressure_effect"]))
    remaining = max(0.0, remaining + model["pressure_effect"] * pressure)
    feed_rate = -min(FEED_RATE_CHANGE_LIMIT_PCT, remaining / model["feed_rate_effect"])
    return ControlChanges(temperature=temperature, pressure=pressure, feed_rate_pct=feed_rate)


def _response_fraction(minute: int, lag_minutes: int) -> float:
    """Editable ramp assumption, with no intervention effect at the origin."""
    if minute == 0:
        return 0.0
    return 1.0 if lag_minutes == 0 else min(1.0, minute / lag_minutes)


def _blend(request: ScenarioRequest, resolved: dict) -> dict | None:
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
    base = {
        key: sum(getattr(tank, key) * tank.share for tank in request.tanks) / total
        for key in ("sulfur", "t95", "cetane", "cost_index")
    }
    additive_fraction = request.additive_pct / 100
    result = {
        # A sulphur-free cetane improver only dilutes sulphur; it does not
        # change the distillation end point of the base blend.
        "sulfur": base["sulfur"] * (1 - additive_fraction),
        "t95": base["t95"],
        "cetane": base["cetane"] + resolved["cetane_gain_per_pct"] * request.additive_pct,
        "cost_index": base["cost_index"] * (1 - additive_fraction) + 100 * additive_fraction,
        "normalized_shares": [
            {"name": tank.name, "share": tank.share / total * 100} for tank in request.tanks
        ],
    }
    result["meets_targets"] = {
        "sulfur": result["sulfur"] <= min(HARD_SULFUR_MAX, request.targets.sulfur_max),
        "t95": result["t95"] <= min(HARD_T95_MAX, request.targets.t95_max),
        "cetane": result["cetane"] >= max(HARD_CETANE_MIN, request.targets.cetane_min),
    }
    result["all_targets_met"] = all(result["meets_targets"].values())
    return result


def _baseline_path(forecast: dict | None, sulfur: float, minutes: list[int]) -> tuple[dict[int, float], str]:
    """No-action ln sulphur at each minute: the model forecast when available, else flat."""
    if forecast and forecast.get("status") == "ok" and forecast.get("horizons"):
        rows = sorted((int(r["minutes"]), log(max(r["prediction"], 0.3))) for r in forecast["horizons"])
        path = {}
        for minute in minutes:
            lower = max((h, v) for h, v in rows if h <= minute)
            upper = min((h, v) for h, v in rows if h >= minute)
            if lower[0] == upper[0]:
                path[minute] = lower[1]
            else:
                weight = (minute - lower[0]) / (upper[0] - lower[0])
                path[minute] = (1 - weight) * lower[1] + weight * upper[1]
        return path, "model_forecast"
    return {minute: log(max(sulfur, 0.3)) for minute in minutes}, "flat_baseline"


def calculate_scenario(directory: Path, request: ScenarioRequest, *, frame: dict | None = None,
                       forecast: dict | None = None) -> dict:
    frame = snapshot(directory, request.at) if frame is None else frame
    values = {item["metric_id"]: item for item in frame["values"]}
    resolved, defaults = resolve_parameters(request.parameters)
    if forecast is None:
        try:
            forecast = forecast_sulfur(directory, request.at, horizon_minutes=request.horizon_minutes)
        except (ForecastUnavailable, ValueError):
            forecast = None
    forecast_ok = bool(forecast and forecast.get("status") == "ok" and forecast.get("nowcast"))

    if request.current_sulfur is not None:
        sulfur, sulfur_source = request.current_sulfur, "request.current_sulfur"
    elif forecast_ok:
        # Best current estimate: lab-anchored analyser nowcast (see REPORT.md);
        # the last laboratory sample may be up to 48 hours old.
        sulfur, sulfur_source = float(forecast["nowcast"]["prediction"]), "model.nowcast"
    else:
        sulfur, sulfur_source = select_sulfur(values)
    t95 = request.current_t95 or _value(values, "lims.ht.2.95%.T")
    cetane = request.current_cetane or _value(values, "lims.ht.2.CetaneNumber")
    if sulfur is None:
        raise ValueError("Нет базового значения серы: задайте его в сценарии")

    observed_feed = _value(values, FEED_SULFUR_METRIC)
    baseline_feed = request.baseline_feed_sulfur or observed_feed or DEFAULT_FEED_SULFUR_WT_PCT
    feed_sulfur = request.feed_sulfur or baseline_feed
    feed_effect = resolved["feed_sulfur_elasticity"] * log(feed_sulfur / baseline_feed)

    minutes = list(range(0, request.horizon_minutes + 1, request.step_minutes))
    if request.horizon_minutes not in minutes:
        minutes.append(request.horizon_minutes)
    # A supplied hypothetical baseline has no forecast dynamics: the model
    # path is calibrated for observed levels, so the what-if stays flat.
    baseline_ln, baseline_kind = _baseline_path(forecast if request.current_sulfur is None else None, sulfur, minutes)
    effective_target = min(request.targets.sulfur_max, HARD_SULFUR_MAX)
    changes = request.changes
    if changes is None:
        response = _response_fraction(request.horizon_minutes, resolved["lag_minutes"])
        # Solve for the requested horizon, not for an unreachable steady state.
        # An editable target cannot relax the confirmed product sulphur limit.
        required = (baseline_ln[request.horizon_minutes] + feed_effect - log(effective_target)) / response if response else 0.0
        changes = _automatic_changes(required, resolved)
    control_effect = (
        resolved["temperature_effect"] * changes.temperature
        + resolved["feed_rate_effect"] * changes.feed_rate_pct
        + resolved["pressure_effect"] * changes.pressure
    )
    target_time = parse_time(request.at)
    trajectory = []
    for minute in minutes:
        response = _response_fraction(minute, resolved["lag_minutes"])
        ln_value = baseline_ln[minute] + feed_effect * response + control_effect * response
        trajectory.append({"minute": minute, "timestamp": (target_time + timedelta(minutes=minute)).isoformat(),
                           "sulfur": exp(ln_value), "baseline_sulfur": exp(baseline_ln[minute])})
    final_ln = baseline_ln[request.horizon_minutes] + feed_effect + control_effect * _response_fraction(request.horizon_minutes, resolved["lag_minutes"])
    steady_state_sulfur = exp(baseline_ln[request.horizon_minutes] + feed_effect + control_effect)
    risk = None
    if forecast_ok or request.current_sulfur is not None:
        try:
            risk = exceedance_at(_load_artifact(), request.horizon_minutes, final_ln)
        except ForecastUnavailable:
            risk = None

    controls = {}
    for metric_id, change, relative, limit in (
        ("ht.T6", changes.temperature, False, TEMPERATURE_CHANGE_LIMIT),
        ("ht.F9", changes.feed_rate_pct, True, FEED_RATE_CHANGE_LIMIT_PCT),
        ("ht.P13", changes.pressure, False, PRESSURE_CHANGE_LIMIT),
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
            "model_change_limit": limit,
        }

    final_sulfur = trajectory[-1]["sulfur"]
    exceedance = risk["exceedance_probability"] if risk else None
    return {
        "at": request.at,
        "horizon_minutes": request.horizon_minutes,
        "step_minutes": request.step_minutes,
        "baseline": {"sulfur": sulfur, "sulfur_source": sulfur_source, "t95": t95, "cetane": cetane,
                     "feed_sulfur": baseline_feed, "feed_sulfur_source": "request" if request.baseline_feed_sulfur else
                     FEED_SULFUR_METRIC if observed_feed else "default", "baseline_kind": baseline_kind},
        "controls": controls,
        "predicted_sulfur": final_sulfur,
        "predicted_sulfur_lower": risk["lower"] if risk else None,
        "predicted_sulfur_upper": risk["upper"] if risk else None,
        "exceedance_probability": exceedance,
        "steady_state_sulfur": steady_state_sulfur,
        "sulfur_target_met": final_sulfur <= request.targets.sulfur_max + TOLERANCE,
        "exceedance_target_met": (exceedance <= request.targets.max_exceedance_probability + TOLERANCE) if exceedance is not None else None,
        "hard_sulfur_limit_met": final_sulfur <= HARD_SULFUR_MAX + TOLERANCE,
        "hard_sulfur_max": HARD_SULFUR_MAX,
        "feed_sulfur": feed_sulfur,
        "feed_effect_ln": feed_effect,
        "control_effect_ln": control_effect,
        "parameters": resolved,
        "parameter_defaults": defaults,
        "prediction_scope": "horizon_endpoint_surrogate",
        "trajectory": trajectory,
        "blend": _blend(request, resolved),
        "assumptions": [
            "Мультипликативная сценарная модель: ln(сера) = ln(база без воздействия) + эластичность×ln(сера сырья/база) + Σ коэффициент×изменение×доля отклика. Не промышленный оптимизатор.",
            "База без воздействия — калиброванный по ЛИМС nowcast/прогноз анализатора, если он доступен; иначе последний источник по приоритету ЛИМС → ПАК → Q21.",
            "Коэффициенты по умолчанию — медианные лагированные отклики анализатора на ступени T6/F9/P13 (наблюдательная оценка, подтверждено только направление для температуры); их можно редактировать.",
            "Лаг — время линейного нарастания эффекта (по умолчанию из ступенчатого отклика), а не подтверждённое время прохождения продукта; значения ЛИМС доступны через 4 часа после отбора пробы.",
            "predicted_sulfur соответствует концу выбранного горизонта; steady_state_sulfur — полному эффекту. Вероятность превышения — из эмпирических остатков прогноза, сдвинутых на эффект сценария.",
            "T95 и цетановое число гидроочистки не прогнозируются: известные базовые значения служат отдельными ограничениями; неизвестные показатели не считаются прошедшими проверку.",
            "Пределы изменения за один шаг (±10 °C, ±10 % расхода, ±0.5 МПа) — модельные допущения прототипа, не паспортные ограничения.",
            "Присадка стоит в 100 раз дороже ДТ; её влияние на цетановое число задаётся параметром эффективности, T95 смеси она не меняет.",
        ],
    }
