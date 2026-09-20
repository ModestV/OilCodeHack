"""Declared experimental proxies; neither failure probabilities nor plant costs."""
from math import isfinite

from .forecast import ForecastUnavailable, _load_artifact


def regime_severity(controls: dict) -> dict:
    """High-side temperature, pressure and feed loading relative to training.

    No failure labels exist. Positive standardized levels are a reproducible
    loading proxy, not a validated relation to equipment life. The cap is the
    forecast's own applicability z-threshold (``max_absolute_z`` in
    ``APPLICABILITY_POLICY``), so a proposed control vector cannot pass this
    gate at a regime the forecast itself would already refuse to score.
    """
    try:
        artifact = _load_artifact()
    except ForecastUnavailable as exc:
        return {"status": "unavailable", "index": None, "factors": [], "reason": str(exc)}
    # Train-only normalisation of the control regime from the applicability
    # (support) model of the longest horizon.
    support = artifact["models"][str(max(int(h) for h in artifact["horizons_minutes"]))]["support"]
    z_cap = float(artifact["applicability_policy"]["max_absolute_z"])
    factors = []
    for metric in ("ht.T6", "ht.P13", "ht.F9"):
        value = controls.get(metric)
        if value is None or not isfinite(value):
            return {"status": "unavailable", "index": None, "factors": factors,
                    "reason": f"Нет достоверного значения {metric} для индекса нагрузки"}
        i = support["feature_columns"].index(metric.split(".")[1])
        mean, scale = support["mean"][i], support["scale"][i]
        high_z = max(0., (value - mean) / scale)
        factors.append({"metric_id": metric, "value": value, "train_mean": mean,
                        "train_std": scale, "positive_z": high_z})
    index = max(f["positive_z"] for f in factors) / z_cap
    return {"status": "ok", "index": index, "class": "high" if index > 2/3 else "elevated" if index > 1/3 else "normal",
            "within_model_limit": index <= 1, "model_limit": 1., "factors": factors,
            "basis": f"max positive z(T6,P13,F9) / {z_cap:g}; train-only normalization, same threshold as forecast applicability",
            "artifact_sha256": artifact["sha256"], "failure_probability": None,
            "assumption": f"Большие T6/P13/F9 условно повышают нагрузку. Порог {z_cap:g}σ — экспериментальный, не промышленный предел."}


def candidate_objectives(scenario: dict, effort: float, max_exceedance_probability: float = 0.3) -> dict:
    controls = scenario["controls"]
    temperature = controls["ht.T6"]["change"]
    pressure = controls["ht.P13"]["change"]
    feed_pct = controls["ht.F9"]["change"]
    severity = regime_severity({key: value["recommended"] for key, value in controls.items()})
    throughput = 1 + feed_pct / 100
    energy = 1 + .10 * temperature / 10 + .05 * pressure / 2 + .10 * feed_pct / 10
    # Off-spec product costs 50-100x the quality margin (organisers), so the
    # residual exceedance probability enters the ranking as an expected-cost
    # proxy.  This is a configured preference, never a permission to violate
    # quality: infeasible candidates are excluded before ranking.
    exceedance = scenario.get("exceedance_probability")
    quality_risk = (exceedance / max_exceedance_probability) if exceedance is not None and max_exceedance_probability > 0 else 0.0
    loss = (0.5 * (1 - throughput) / .10 + .25 * (energy - 1) / .25
            + .25 * severity["index"] + .05 * effort + .5 * quality_risk) if severity["index"] is not None else None
    return {"throughput_index": throughput, "throughput_change_pct": feed_pct,
            "energy_cost_index": energy, "regime_severity": severity,
            "exceedance_probability": exceedance, "quality_risk_index": quality_risk,
            "ranking_loss": loss,
            "ranking_formula": "0.5*(1-throughput)/0.10 + 0.25*(energy-1)/0.25 + 0.25*severity + 0.05*effort + 0.5*P(>10)/P_max",
            "basis": "scenario assumptions; throughput assumes unchanged yield; energy has no monetary units; P(>10) from forecast residuals",
            "energy_formula": "1 + 0.10*delta_T/10 + 0.05*delta_P/2 + 0.10*delta_feed_pct/10"}
