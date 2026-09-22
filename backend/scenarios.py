"""Transparent, editable decision-support scenarios for the hackathon prototype."""

from __future__ import annotations

from datetime import timedelta
from math import exp, log
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analytics import parse_time, snapshot
from .forecast import ForecastUnavailable, control_response_defaults, forecast_sulfur


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
MAX_ADDITIVE_PCT = 3.0
# Model limit on one recommendation step (≤ 60 min): ≈ 99th percentile of the
# absolute 60-minute change of T6/P13/F9 in normal operation 2023–2025
# (6.9 °C, 0.22 MPa, 11.5%).  The observational response coefficients carry no
# evidence beyond moves the history contains.  Not a plant rate limit.
STEP_LIMITS = {"temperature": 6.0, "feed_rate_pct": 10.0, "pressure": 0.2}
# Used when the model artifact carries no alarm threshold for the horizon.
DEFAULT_MAX_EXCEEDANCE_PROBABILITY = 0.2


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
    # Charge a sulphur-raising move at the larger of the editable coefficient and
    # the observed step response, credit a sulphur-lowering move at the smaller.
    asymmetric_response: bool = True


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
    # Expert guidance: keep 1–2 ppm below the 10 mg/kg cap. Applied as a point
    # margin when no calibrated forecast uncertainty describes the baseline.
    sulfur_margin_mgkg: float = Field(default=1.0, ge=0, lt=10)
    # Accepted P(S > 10) at the horizon; None = the model's calibrated alarm threshold.
    max_exceedance_probability: float | None = Field(default=None, gt=0, lt=1)
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
    temperature = min(STEP_LIMITS["temperature"], required_reduction / abs(model.temperature_effect))
    remaining = max(0.0, required_reduction + model.temperature_effect * temperature)
    pressure = min(STEP_LIMITS["pressure"], remaining / abs(model.pressure_effect))
    remaining = max(0.0, remaining + model.pressure_effect * pressure)
    feed_rate = -min(STEP_LIMITS["feed_rate_pct"], remaining / model.feed_rate_effect)
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


def forecast_covers_path(forecast: dict | None, horizon: int) -> bool:
    return bool(forecast and forecast.get("status") == "ok" and forecast.get("path_supported")
                and forecast.get("nowcast") and forecast.get("horizon_minutes") == horizon)


def risk_policy(request: ScenarioRequest, forecast: dict | None) -> dict:
    """How much sulphur risk a candidate may carry at the horizon endpoint.

    With an observed forecast the model's out-of-fold ln-residual quantiles give
    P(S > 10) for any shifted endpoint; the admissible level is the one where this
    probability equals the calibrated alarm threshold (≈ 8.85 mg/kg at 3 h, i.e.
    the expert's 1–2 ppm margin).  A manual baseline has no calibrated error, so
    the explicit point margin is used instead.  ``design_level`` (half the
    admissible probability, or 1.5 margins) is the middle of the admissible
    zone: the automatic correction aims there, and a cost-saving move (one
    that raises sulphur) must end inside it; holding the regime or a move that
    lowers the risk only has to stay within the admissible probability.
    """
    quantiles = (forecast or {}).get("risk_quantiles") if request.current_sulfur is None else None
    usable = (forecast_covers_path(forecast, request.horizon_minutes) and quantiles
              and len(quantiles.get("quantiles") or []) >= 3
              and len(quantiles["quantiles"]) == len(quantiles.get("probabilities") or []))
    if usable:
        p_max = float(request.max_exceedance_probability or forecast.get("alarm_probability")
                      or DEFAULT_MAX_EXCEEDANCE_PROBABILITY)
        probabilities = np.asarray(quantiles["probabilities"], dtype=float)
        residuals = np.asarray(quantiles["quantiles"], dtype=float)

        def level(p: float) -> float:
            return HARD_SULFUR_MAX * exp(-float(np.interp(1 - p, probabilities, residuals)))

        return {"basis": "calibrated_residual_quantiles", "max_exceedance_probability": p_max,
                "design_max_exceedance_probability": p_max / 2,
                "safe_level": level(p_max), "design_level": level(p_max / 2),
                "quantiles": quantiles, "margin_mgkg": None}
    return {"basis": "point_margin", "max_exceedance_probability": None, "design_max_exceedance_probability": None,
            "safe_level": HARD_SULFUR_MAX - request.sulfur_margin_mgkg,
            "design_level": HARD_SULFUR_MAX - 1.5 * request.sulfur_margin_mgkg,
            "quantiles": None, "margin_mgkg": request.sulfur_margin_mgkg}


def candidate_risk(policy: dict, sulfur: float) -> dict:
    """Risk of one endpoint under ``policy``; P is None without a calibrated error model."""
    public = {k: v for k, v in policy.items() if k != "quantiles"}
    if policy["basis"] != "calibrated_residual_quantiles":
        return {**public, "sulfur": sulfur, "exceedance_probability": None, "upper_80": None,
                "passed": sulfur <= policy["safe_level"] + 1e-9,
                "within_design": sulfur <= policy["design_level"] + 1e-9}
    from tools.modeling.anchored_features import exceedance_probability, interval
    ln_sulfur = log(max(sulfur, 1e-3))
    probability = exceedance_probability(policy["quantiles"], ln_sulfur, log(HARD_SULFUR_MAX))
    return {**public, "sulfur": sulfur, "exceedance_probability": probability,
            "upper_80": interval(policy["quantiles"], ln_sulfur)[1],
            "passed": probability <= policy["max_exceedance_probability"] + 1e-9,
            "within_design": probability <= policy["design_max_exceedance_probability"] + 1e-9}


_OBSERVED_RESPONSE: dict | None = None


def observed_response() -> dict | None:
    """Median ln-sulphur step responses of the analyser to T6/F9/P13 (artifact v3), cached."""
    global _OBSERVED_RESPONSE
    if _OBSERVED_RESPONSE is None:
        defaults = control_response_defaults()
        _OBSERVED_RESPONSE = {} if defaults is None else {
            "temperature": defaults["temperature_effect"], "feed_rate_pct": defaults["feed_rate_effect"],
            "pressure": defaults["pressure_effect"], "basis": defaults["basis"]}
    return _OBSERVED_RESPONSE or None


def control_effects(changes: ControlChanges, parameters: ModelParameters, level: float) -> dict:
    """Per-control sulphur effect (mg/kg) at the full response.

    The editable coefficients are 3–12 times smaller than the median observed
    step response, which is prudent when crediting a correction and imprudent
    when judging a cost-saving move.  With ``asymmetric_response`` every
    control takes whichever of the two estimates is worse for quality.
    """
    observed = observed_response() if parameters.asymmetric_response else None
    effects = {}
    for name, coefficient in (("temperature", parameters.temperature_effect),
                              ("feed_rate_pct", parameters.feed_rate_effect),
                              ("pressure", parameters.pressure_effect)):
        change = getattr(changes, name)
        editable = coefficient * change
        measured = observed[name] * change * max(level, 0.0) if observed else None
        effects[name] = {"editable": editable, "observed": measured,
                         "used": editable if measured is None else max(editable, measured)}
    return effects


def _baseline_knots(forecast: dict | None, sulfur: float, horizon: int) -> list[tuple[float, float]]:
    """Scenario interpolation is linear in concentration; exact requested endpoint is retained."""
    if forecast_covers_path(forecast, horizon):
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
    forecast_ok = forecast_covers_path(forecast, request.horizon_minutes)
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
    policy = risk_policy(request, forecast)

    feed_effect = request.parameters.feed_sulfur_transfer * (
        request.feed_sulfur - request.baseline_feed_sulfur
    )
    changes = request.changes
    if changes is None:
        response = _response_fraction(request.horizon_minutes, request.parameters.lag_minutes, request.parameters.dead_time_minutes)
        # Solve for the requested horizon, not for an unreachable steady state.
        # An editable target cannot relax the confirmed product sulphur limit,
        # and the solution aims inside the risk margin rather than at 10 itself.
        effective_target = min(request.targets.sulfur_max, HARD_SULFUR_MAX, policy["design_level"])
        if request.tanks:
            effective_target = request.intermediate_sulfur_max if request.intermediate_sulfur_max is not None else endpoint + max(0, feed_effect)
        changes = _automatic_changes(
            (endpoint - effective_target) / response + feed_effect if response else 0,
            request.parameters,
        )
    effects = control_effects(changes, request.parameters, endpoint)
    full_effect = feed_effect + sum(item["used"] for item in effects.values())
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
    additive = None
    if blend is None and request.additive_pct > 0:
        # Direct route: the hydrotreated product is dosed with cetane improver.
        # Unknown additive sulphur is taken at the 10 mg/kg product cap, which
        # never credits dilution below the hydrotreater result.
        fraction = request.additive_pct / 100
        additive_sulfur = request.parameters.additive_sulfur_mgkg
        product_sulfur = final_sulfur * (1 - fraction) + (HARD_SULFUR_MAX if additive_sulfur is None else additive_sulfur) * fraction
        additive = {
            "pct": request.additive_pct, "dose_kg_t": request.additive_pct * 10,
            "cetane_gain": request.parameters.cetane_gain_per_pct * request.additive_pct,
            "product_cetane": (cetane + request.parameters.cetane_gain_per_pct * request.additive_pct
                               if cetane is not None else None),
            "cost_index": (1 - fraction) + 100 * fraction,
            "sulfur_basis": ("explicit_additive_sulfur" if additive_sulfur is not None
                             else "assumed_10_mg_kg_no_dilution_credit"),
        }
    risk = (candidate_risk(policy, product_sulfur) if blend is None
            else {**{k: v for k, v in policy.items() if k != "quantiles"}, "basis": "blend_mass_balance",
                  "sulfur": product_sulfur, "exceedance_probability": None, "upper_80": None, "passed": None})
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
        "control_effects": {**effects, "basis": ("worse of editable coefficient and observed median step response "
                                                 "(ln per unit × baseline endpoint)" if request.parameters.asymmetric_response
                                                 else "editable coefficients")},
        "batch": {"arriving_minutes": arriving_duration, "produced_t": produced_mass,
                  "produced_sulfur": produced_sulfur, "withdrawal_at_horizon": True},
        "hard_sulfur_max": HARD_SULFUR_MAX,
        "prediction_scope": "horizon_endpoint_surrogate",
        "trajectory": trajectory,
        "blend": blend,
        "additive": additive,
        "risk": risk,
        "applied_recipe": {"tanks": [t.model_dump() for t in request.tanks], "additive_pct": request.additive_pct},
        "assumptions": [
            "Простая линейная сценарная модель, не промышленный оптимизатор.",
            "Для наблюдаемого режима база — калиброванный прогноз H0/H1/H2/H3; между опорными точками линейная интерполяция концентрации. Качество поступления интегрируется до горизонта минус доставка, независимо от шага графика.",
            "Коэффициенты чувствительности и пределы изменения являются редактируемыми модельными допущениями. По умолчанию изменение, повышающее серу, оценивается по большему из редактируемого коэффициента и медианного наблюдаемого отклика анализатора на ступени T6/F9/P13, а снижающее — по меньшему.",
            "Параметр lag задаёт время линейного нарастания эффекта (0–180 минут), а не подтверждённое время прохождения продукта; значения ЛИМС доступны через 4 часа после отбора пробы.",
            "predicted_sulfur соответствует концу выбранного горизонта; steady_state_sulfur — полному условному эффекту. Проверка конечной точки не гарантирует качество на всём переходе.",
            "T95 и цетановое число гидроочистки не прогнозируются: известные базовые значения служат отдельными ограничениями; неизвестные показатели не считаются прошедшими проверку.",
            "Присадка стоит в 100 раз дороже ДТ; её влияние на цетановое число задаётся параметром эффективности.",
            "Предел 10 мг/кг относится к товарному продукту. Отдельный предел гидроочистки задаётся явно.",
            "Новая партия полностью перемешивается с запасом перед отбором смеси в конце горизонта; непрерывная отгрузка не моделируется. T95 и цетан новой партии задаются пользователем.",
            "Выпуск т/ч задаётся отдельно: спорный объёмный расход F15 не используется для массового баланса. Массовый выпуск меняется пропорционально изменению подачи при неизменном условном выходе.",
            "Дозировка 0–3% — модельный диапазон (0–30 кг/т), не установленный технологический предел. Присадке не приписывается снижение T95.",
            "Запас по сере: при прогнозе по наблюдениям вариант допустим, если P(S > 10) в конце горизонта не выше порога тревоги модели (≈ 8,85 мг/кг на 3 ч); при ручной базе — если сера не выше 10 минус заданный запас (1 мг/кг по умолчанию). Автоматический вариант целится в середину допустимой зоны; экономичный вариант (повышающий серу ради экономии) должен заканчиваться в её середине (P ≤ половины порога или сера ≤ 10 − 1,5 запаса); удержание и снижающие риск варианты — в пределах порога. Шаг изменения ограничен модельным пределом: T6 ±6 °C, P13 ±0,2 МПа, F9 ±10% (≈ 99-й перцентиль часовых изменений 2023–2025).",
            "Без смеси присадка дозируется в гидроочищенный продукт; неизвестная сера присадки принимается равной 10 мг/кг, поэтому разбавление не засчитывается.",
        ],
    }
