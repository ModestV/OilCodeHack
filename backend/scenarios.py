"""Transparent, editable decision-support scenarios for the hackathon prototype.

The hydro-treatment surrogate is multiplicative (log-domain): every control
move scales product sulphur by ``exp(coefficient * change)`` with a linear
ramp over the control's own ``lag_minutes``.  The no-action path is the
two-stage forecast (``backend/forecast.py``), which already reflects control
moves the operator has made before the origin; a scenario adds only the
*proposed* change relative to the current control values, so an in-flight
move is never counted twice.  The default coefficients are the median lagged
responses estimated from online-analyser step events on data before the
model's ``fit_end`` (``reports/modeling/sulfur-forecast/model.json`` →
``walk_forward[].control_response``) with bootstrap standard errors; the
expert confirmed only the direction for temperature, so the numbers stay
editable assumptions, not certified plant gains.  Their uncertainty widens
the interval and P(>10) of a candidate in proportion to the proposed move.
"""

from __future__ import annotations

from datetime import timedelta
from math import exp, log, sqrt
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import parse_time, snapshot
from .forecast import (
    ForecastUnavailable,
    _load_artifact,
    applicable_model,
    exceedance_at,
    forecast_sulfur,
)

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
CONTROL_KEYS = {"T6": "temperature_effect", "F9": "feed_rate_effect", "P13": "pressure_effect"}
CONTROL_METRIC_IDS = {"T6": "ht.T6", "F9": "ht.F9", "P13": "ht.P13"}
# Fallback defaults (rounded train-only step-response estimates of the
# shipped artifact); ``scenario_defaults()`` reads the live values.
FALLBACK_RESPONSE = {"temperature_effect": -0.029, "feed_rate_effect": 0.018, "pressure_effect": -0.23,
                     "lag_minutes": 120, "lags": {"T6": 120, "F9": 60, "P13": 60},
                     "uncertainty": {"temperature_effect": 0.002, "feed_rate_effect": 0.003, "pressure_effect": 0.06}}
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
    """Editable surrogate coefficients; ``None`` means "use the estimated default".

    ``lag_minutes`` overrides the ramp of every control; by default each
    control uses the lag estimated from its own step events.
    """

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

    def as_controls(self) -> dict[str, float]:
        return {"T6": self.temperature, "F9": self.feed_rate_pct, "P13": self.pressure}


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


def scenario_defaults(at: str | None = None) -> dict:
    """Effective default coefficients (train-only step responses of the model applicable at ``at``) with their evidence."""
    try:
        if at:
            artifact, entry = applicable_model(at)
        else:
            artifact = _load_artifact()
            entry = artifact["walk_forward"][-1]
        if entry is None:
            # Before the first model: report the earliest coefficients as
            # defaults; the forecast itself abstains for such origins.
            entry = artifact["walk_forward"][0]
        response = entry.get("control_response") or {}
        if all(response.get(k, {}).get("coefficient") is not None for k in ("T6", "F9", "P13")):
            lags = {k: int(response[k].get("lag_minutes") or 60) for k in ("T6", "F9", "P13")}
            return {
                "temperature_effect": float(response["T6"]["coefficient"]),
                "feed_rate_effect": float(response["F9"]["coefficient"]),
                "pressure_effect": float(response["P13"]["coefficient"]),
                "lag_minutes": max(lags.values()), "lags": lags,
                "uncertainty": {CONTROL_KEYS[k]: float(response[k].get("se") or 0.0) for k in ("T6", "F9", "P13")},
                "ci_80": {CONTROL_KEYS[k]: response[k].get("ci_80") for k in ("T6", "F9", "P13")},
                "feed_sulfur_elasticity": 1.0,
                "units": {"temperature_effect": "ln(мг/кг) на °C", "feed_rate_effect": "ln(мг/кг) на % расхода",
                          "pressure_effect": "ln(мг/кг) на МПа", "feed_sulfur_elasticity": "ln S_out на ln S_in"},
                "events": {k: int(response[k].get("events", 0)) for k in ("T6", "F9", "P13")},
                "basis": "медианный лагированный отклик ln(серы) анализатора Q21 на ступени одного тега (наблюдательная оценка, "
                         "только данные до fit_end модели; se — bootstrap по событиям)",
                "model_fit_end": entry.get("fit_end"), "applicable_at": at,
                "consistency": entry.get("consistency"),
                "source": "reports/modeling/sulfur-forecast/model.json", "artifact_sha256": artifact["sha256"],
            }
    except ForecastUnavailable:
        pass
    return {**FALLBACK_RESPONSE, "feed_sulfur_elasticity": 1.0, "basis": "встроенные умолчания (артефакт недоступен)",
            "source": None, "model_fit_end": None, "applicable_at": at}


def resolve_parameters(parameters: ModelParameters, at: str | None = None) -> tuple[dict, dict]:
    defaults = scenario_defaults(at)
    lags = {k: (parameters.lag_minutes if parameters.lag_minutes is not None else defaults["lags"][k]) for k in ("T6", "F9", "P13")}
    resolved = {
        "lag_minutes": parameters.lag_minutes if parameters.lag_minutes is not None else defaults["lag_minutes"],
        "lags": lags,
        "feed_sulfur_elasticity": parameters.feed_sulfur_elasticity,
        "temperature_effect": parameters.temperature_effect if parameters.temperature_effect is not None else defaults["temperature_effect"],
        "feed_rate_effect": parameters.feed_rate_effect if parameters.feed_rate_effect is not None else defaults["feed_rate_effect"],
        "pressure_effect": parameters.pressure_effect if parameters.pressure_effect is not None else defaults["pressure_effect"],
        "cetane_gain_per_pct": parameters.cetane_gain_per_pct,
        # Standard errors of the default coefficients; an edited coefficient
        # keeps the default's uncertainty as a proxy (no better evidence).
        "uncertainty": dict(defaults.get("uncertainty") or {}),
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


def select_baseline(request: ScenarioRequest, values: dict, forecast: dict | None) -> tuple[float | None, str | None, dict | None]:
    """Baseline sulphur, its source and evidence item — shared by the scenario and the quality agent.

    Priority: an explicit hypothetical value, the lab-anchored nowcast of a
    usable forecast, then the raw source priority LIMS → PAK → Q21.
    """
    if request.current_sulfur is not None:
        return request.current_sulfur, "request.current_sulfur", None
    if forecast and forecast.get("status") == "ok" and forecast.get("nowcast"):
        nowcast = forecast["nowcast"]
        item = {"value": float(nowcast["prediction"]), "timestamp": forecast.get("feature_time"),
                "available_at": forecast.get("feature_time"), "freshness": "fresh", "flags": [],
                "lower": nowcast.get("lower"), "upper": nowcast.get("upper"),
                "exceedance_probability": nowcast.get("exceedance_probability")}
        return float(nowcast["prediction"]), "model.nowcast", item
    value, source = select_sulfur(values)
    return value, source, values.get(source) if source else None


def _response_fraction(minute: int, lag_minutes: int) -> float:
    """Editable ramp assumption, with no intervention effect at the origin."""
    if minute == 0:
        return 0.0
    return 1.0 if lag_minutes == 0 else min(1.0, minute / lag_minutes)


def _ramps(minute: int, lags: dict[str, int]) -> dict[str, float]:
    return {k: _response_fraction(minute, lag) for k, lag in lags.items()}


def _automatic_changes(required_ln_reduction: float, model: dict, ramps: dict[str, float]) -> ControlChanges:
    """Cheapest-first split of a required ln reduction at the horizon: temperature, then pressure, then feed.

    ``ramps`` are the response fractions of each control at the horizon; a
    control whose response has not started cannot contribute.
    """
    if required_ln_reduction <= 0:
        return ControlChanges()
    remaining = required_ln_reduction
    temperature = pressure = feed_rate = 0.0
    if ramps["T6"] > 0:
        temperature = min(TEMPERATURE_CHANGE_LIMIT, remaining / (abs(model["temperature_effect"]) * ramps["T6"]))
        remaining = max(0.0, remaining + model["temperature_effect"] * temperature * ramps["T6"])
    if ramps["P13"] > 0 and remaining > TOLERANCE:
        pressure = min(PRESSURE_CHANGE_LIMIT, remaining / (abs(model["pressure_effect"]) * ramps["P13"]))
        remaining = max(0.0, remaining + model["pressure_effect"] * pressure * ramps["P13"])
    if ramps["F9"] > 0 and remaining > TOLERANCE:
        feed_rate = -min(FEED_RATE_CHANGE_LIMIT_PCT, remaining / (model["feed_rate_effect"] * ramps["F9"]))
    return ControlChanges(temperature=temperature, pressure=pressure, feed_rate_pct=feed_rate)


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
    """No-action ln sulphur at each minute: the model forecast when available, else flat.

    The forecast path already includes the effect of control moves made
    before the origin (Stage 1 sees the recent T6/F9/P13 deltas).
    """
    if forecast and forecast.get("status") == "ok" and forecast.get("horizons"):
        rows = sorted((int(r["minutes"]), log(max(r["prediction"], 0.3))) for r in forecast["horizons"]
                      if r.get("prediction") is not None and r.get("status", "ok") == "ok")
        if rows and rows[0][0] == 0 and rows[-1][0] >= max(minutes):
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
    resolved, defaults = resolve_parameters(request.parameters, request.at)
    if forecast is None:
        try:
            forecast = forecast_sulfur(directory, request.at, horizon_minutes=request.horizon_minutes)
        except (ForecastUnavailable, ValueError):
            forecast = None
    forecast_ok = bool(forecast and forecast.get("status") == "ok" and forecast.get("nowcast"))

    sulfur, sulfur_source, _ = select_baseline(request, values, forecast)
    t95 = request.current_t95 or _value(values, "lims.ht.2.95%.T")
    cetane = request.current_cetane or _value(values, "lims.ht.2.CetaneNumber")
    if sulfur is None:
        raise ValueError("Нет базового значения серы: задайте его в сценарии")

    observed_feed = _value(values, FEED_SULFUR_METRIC)
    baseline_feed = request.baseline_feed_sulfur or observed_feed or DEFAULT_FEED_SULFUR_WT_PCT
    feed_sulfur = request.feed_sulfur or baseline_feed
    feed_effect = resolved["feed_sulfur_elasticity"] * log(feed_sulfur / baseline_feed)
    lags = resolved["lags"]

    minutes = list(range(0, request.horizon_minutes + 1, request.step_minutes))
    if request.horizon_minutes not in minutes:
        minutes.append(request.horizon_minutes)
    # A supplied hypothetical baseline has no forecast dynamics: the model
    # path is calibrated for observed levels, so the what-if stays flat.
    baseline_ln, baseline_kind = _baseline_path(forecast if request.current_sulfur is None else None, sulfur, minutes)
    effective_target = min(request.targets.sulfur_max, HARD_SULFUR_MAX)
    horizon = request.horizon_minutes
    ramps_at_horizon = _ramps(horizon, lags)
    # Feed sulphur travels with the feed: it ramps like the feed-rate response.
    feed_ramp_at_horizon = ramps_at_horizon["F9"]
    changes = request.changes
    if changes is None:
        # Solve for the requested horizon, not for an unreachable steady state.
        # An editable target cannot relax the confirmed product sulphur limit.
        required = baseline_ln[horizon] + feed_effect * feed_ramp_at_horizon - log(effective_target)
        changes = _automatic_changes(required, resolved, ramps_at_horizon)
    coefficients = {"T6": resolved["temperature_effect"], "F9": resolved["feed_rate_effect"], "P13": resolved["pressure_effect"]}
    moves = changes.as_controls()
    control_effect = sum(coefficients[k] * moves[k] for k in coefficients)
    target_time = parse_time(request.at)
    trajectory = []
    for minute in minutes:
        ramps = _ramps(minute, lags)
        ln_value = baseline_ln[minute] + feed_effect * ramps["F9"] + sum(coefficients[k] * moves[k] * ramps[k] for k in coefficients)
        trajectory.append({"minute": minute, "timestamp": (target_time + timedelta(minutes=minute)).isoformat(),
                           "sulfur": exp(ln_value), "baseline_sulfur": exp(baseline_ln[minute])})
    # The endpoint uses exactly the trajectory formula: point estimate,
    # interval and P(>10) are all derived from the same ln value.
    final_ln = (baseline_ln[horizon] + feed_effect * feed_ramp_at_horizon
                + sum(coefficients[k] * moves[k] * ramps_at_horizon[k] for k in coefficients))
    steady_state_sulfur = exp(baseline_ln[horizon] + feed_effect + control_effect)
    uncertainty = resolved.get("uncertainty") or {}
    extra_sigma = sqrt(sum((uncertainty.get(CONTROL_KEYS[k], 0.0) * moves[k] * ramps_at_horizon[k]) ** 2 for k in coefficients))
    risk = None
    if forecast_ok or request.current_sulfur is not None:
        try:
            _, entry = applicable_model(request.at)
            if entry is not None:
                persistence = bool(forecast_ok and (forecast.get("stage1") or {}).get("status") == "fallback_persistence")
                risk = exceedance_at(entry, horizon, final_ln, extra_sigma=extra_sigma, persistence=persistence,
                                     hard_limit=HARD_SULFUR_MAX)
        except ForecastUnavailable:
            risk = None

    controls = {}
    for key, metric_id, relative, limit in (
        ("T6", "ht.T6", False, TEMPERATURE_CHANGE_LIMIT),
        ("F9", "ht.F9", True, FEED_RATE_CHANGE_LIMIT_PCT),
        ("P13", "ht.P13", False, PRESSURE_CHANGE_LIMIT),
    ):
        current = _value(values, metric_id)
        change = moves[key]
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
            "lag_minutes": lags[key],
            "response_fraction_at_horizon": ramps_at_horizon[key],
            "effect_ln_at_horizon": coefficients[key] * change * ramps_at_horizon[key],
        }

    final_sulfur = trajectory[-1]["sulfur"]
    exceedance = risk["exceedance_probability"] if risk else None
    return {
        "at": request.at,
        "horizon_minutes": request.horizon_minutes,
        "step_minutes": request.step_minutes,
        "baseline": {"sulfur": sulfur, "sulfur_source": sulfur_source, "t95": t95, "cetane": cetane,
                     "feed_sulfur": baseline_feed, "feed_sulfur_source": "request" if request.baseline_feed_sulfur else
                     FEED_SULFUR_METRIC if observed_feed else "default", "baseline_kind": baseline_kind,
                     "in_flight_controls": (forecast or {}).get("in_flight_controls") if forecast_ok else None},
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
        "control_effect_ln_at_horizon": final_ln - baseline_ln[horizon] - feed_effect * feed_ramp_at_horizon,
        "coefficient_uncertainty": uncertainty,
        "interval_widening_ln_sigma": extra_sigma,
        "parameters": resolved,
        "parameter_defaults": defaults,
        "prediction_scope": "horizon_endpoint_surrogate",
        "trajectory": trajectory,
        "blend": _blend(request, resolved),
        "assumptions": [
            "Мультипликативная сценарная модель: ln(сера) = ln(база без воздействия) + эластичность×ln(сера сырья/база)×доля отклика + "
            "Σ коэффициент×изменение×доля отклика тега. Не промышленный оптимизатор.",
            "База без воздействия — двухступенчатый прогноз (динамика анализатора + калибровка по ЛИМС), если он доступен; "
            "она уже учитывает изменения T6/F9/P13, сделанные до момента решения, а изменение кандидата задаётся относительно текущих значений, "
            "поэтому уже сделанный ход не учитывается дважды. Иначе — последний источник по приоритету ЛИМС → ПАК → Q21.",
            "Коэффициенты по умолчанию — медианные лагированные отклики анализатора на ступени T6/F9/P13 по данным до fit_end модели "
            "(наблюдательная оценка, подтверждено только направление для температуры); их можно редактировать. "
            "Стандартные ошибки коэффициентов расширяют интервал и вероятность превышения пропорционально размеру изменения.",
            "Лаг — время линейного нарастания эффекта отдельно для каждого тега (по умолчанию из ступенчатого отклика), а не подтверждённое "
            "время прохождения продукта; значения ЛИМС доступны через 4 часа после отбора пробы.",
            "predicted_sulfur соответствует концу выбранного горизонта; steady_state_sulfur — полному эффекту. Точечная оценка, интервал и "
            "вероятность превышения считаются от одного и того же значения ln в конце горизонта.",
            "T95 и цетановое число гидроочистки не прогнозируются: известные базовые значения служат отдельными ограничениями; "
            "неизвестные показатели не считаются прошедшими проверку.",
            "Пределы изменения за один шаг (±10 °C, ±10 % расхода, ±0.5 МПа) — модельные допущения прототипа, не паспортные ограничения.",
            "Присадка стоит в 100 раз дороже ДТ; её влияние на цетановое число задаётся параметром эффективности, T95 смеси она не меняет.",
        ],
    }
