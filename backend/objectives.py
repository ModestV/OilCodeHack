"""Declared experimental proxies; neither failure probabilities nor plant costs."""
from math import isfinite

# Freeze the original load proxy reference distribution across forecast upgrades.
# Its engineering score is independent of the active sulphur model's support gate.
from .forecast_legacy import ForecastUnavailable, _load_artifact


def annotate_pareto(candidates: list[dict]) -> None:
    """Compare feasible candidates with complete objectives; never alter ranking.

    Adapted from the v4 branch. Missing objectives are NOT zero cost/risk.
    """
    vectors = {}
    for item in candidates:
        item.update(pareto=None, dominated_by=[], pareto_status="infeasible")
        if not item.get("feasible") or not item.get("safety_gate", {}).get("passed"):
            continue
        objectives = item.get("objectives") or {}
        values = [objectives.get("throughput_index"), objectives.get("energy_cost_index"),
                  (objectives.get("regime_severity") or {}).get("index"),
                  objectives.get("blend_cost_index"), item.get("effort")]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not isfinite(v) for v in values):
            item["pareto_status"] = "incomplete_objectives"
            continue
        vectors[item["id"]] = [-values[0], *values[1:]]
        item["pareto_status"] = "evaluated"
    for item in candidates:
        if item["id"] not in vectors:
            continue
        current = vectors[item["id"]]
        dominators = [key for key, other in vectors.items() if key != item["id"]
                      and all(a <= b for a, b in zip(other, current))
                      and any(a < b for a, b in zip(other, current))]
        item.update(pareto=not dominators, dominated_by=dominators)


def regime_severity(controls: dict) -> dict:
    """High-side temperature, pressure and feed loading relative to training.

    No failure labels exist. Positive standardized levels are a reproducible
    loading proxy, not a validated relation to equipment life. The 12-sigma
    experimental cap is the frozen first-iteration convention, not the new forecast gate.
    """
    try:
        artifact = _load_artifact()
    except ForecastUnavailable as exc:
        return {"status": "unavailable", "index": None, "factors": [], "reason": str(exc)}
    factors = []
    for metric in ("ht.T6", "ht.P13", "ht.F9"):
        value = controls.get(metric)
        if value is None or not isfinite(value):
            return {"status": "unavailable", "index": None, "factors": factors,
                    "reason": f"Нет достоверного значения {metric} для индекса нагрузки"}
        i = artifact["feature_columns"].index("242000__" + metric.split(".")[1])
        mean, scale = artifact["mean"][i], artifact["scale"][i]
        high_z = max(0., (value - mean) / scale)
        factors.append({"metric_id": metric, "value": value, "train_mean": mean,
                        "train_std": scale, "positive_z": high_z})
    index = max(f["positive_z"] for f in factors) / 12.
    return {"status": "ok", "index": index, "class": "high" if index > 2/3 else "elevated" if index > 1/3 else "normal",
            "within_model_limit": index <= 1, "model_limit": 1., "factors": factors,
            "basis": "max positive z(T6,P13,F9) / 12; train-only normalization",
            "artifact_sha256": artifact["sha256"], "failure_probability": None,
            "assumption": "Большие T6/P13/F9 условно повышают нагрузку. Порог 12σ — экспериментальный, не промышленный предел."}


SHUTDOWN_FEED_FRACTION = 0.10
TRANSITION_FEED_FRACTION = 0.60


def operating_state(controls: dict) -> dict:
    """Shutdown / start-up detection from the feed rate F9.

    History contains long stops with F9 ≈ 0 (e.g. April 2024, June 2026) and
    ramps between them.  Below 10% of the training mean the unit is treated as
    stopped; below 60% (under the 5th percentile of history) as a start-up,
    shutdown or deep turndown transition.  Transient regimes are not modelled,
    so the contour refuses to advise in both states.  Thresholds are explicit
    prototype assumptions, not plant limits.
    """

    value = controls.get("ht.F9")
    if value is None or not isfinite(value):
        return {"state": "unknown", "feed": None, "feed_fraction_of_train_mean": None,
                "reason": "Нет значения F9 для определения режима установки"}
    try:
        artifact = _load_artifact()
        mean = artifact["mean"][artifact["feature_columns"].index("242000__F9")]
    except (ForecastUnavailable, KeyError, ValueError) as exc:
        return {"state": "unknown", "feed": value, "feed_fraction_of_train_mean": None, "reason": str(exc)}
    fraction = value / mean
    state = ("shutdown" if fraction < SHUTDOWN_FEED_FRACTION
             else "transition" if fraction < TRANSITION_FEED_FRACTION else "normal")
    reason = {
        "shutdown": f"Установка в режиме останова: подача F9 {value:.1f} — {max(fraction, 0):.0%} от среднего обучения",
        "transition": f"Режим пуска/останова или глубокого снижения нагрузки: подача F9 {value:.1f} — "
                      f"{max(fraction, 0):.0%} от среднего обучения; переходные режимы не моделируются",
        "normal": None,
    }[state]
    return {"state": state, "feed": value, "feed_fraction_of_train_mean": fraction, "train_mean": mean,
            "thresholds": {"shutdown": SHUTDOWN_FEED_FRACTION, "transition": TRANSITION_FEED_FRACTION},
            "reason": reason, "basis": "feed fraction of train mean; prototype assumption"}


def candidate_objectives(scenario: dict, effort: float) -> dict:
    controls = scenario["controls"]
    temperature = controls["ht.T6"]["change"]
    pressure = controls["ht.P13"]["change"]
    feed_pct = controls["ht.F9"]["change"]
    severity = regime_severity({key: value["recommended"] for key, value in controls.items()})
    throughput = 1 + feed_pct / 100
    energy = 1 + .10 * temperature / 10 + .05 * pressure / 2 + .10 * feed_pct / 10
    # This is a configured preference, never a permission to violate quality.
    loss = (0.5 * (1 - throughput) / .10 + .25 * (energy - 1) / .25
            + .25 * severity["index"] + .05 * effort) if severity["index"] is not None else None
    blend_cost = scenario["blend"]["cost_index"] if scenario.get("blend") else 1.
    if loss is not None:
        loss += .25 * (blend_cost - 1)
    return {"throughput_index": throughput, "throughput_change_pct": feed_pct,
            "energy_cost_index": energy, "regime_severity": severity,
            "ranking_loss": loss,
            "blend_cost_index": blend_cost,
            "ranking_formula": "0.5*(1-throughput)/0.10 + 0.25*(energy-1)/0.25 + 0.25*severity + 0.05*effort + 0.25*(blend_cost-1)",
            "basis": "scenario assumptions; throughput assumes unchanged yield; energy has no monetary units",
            "energy_formula": "1 + 0.10*delta_T/10 + 0.05*delta_P/2 + 0.10*delta_feed_pct/10"}
