"""Declared experimental proxies; neither failure probabilities nor plant costs."""
from math import isfinite

from .forecast import ForecastUnavailable, _load_artifact


def regime_severity(controls: dict) -> dict:
    """High-side temperature, pressure and feed loading relative to training.

    No failure labels exist. Positive standardized levels are a reproducible
    loading proxy, not a validated relation to equipment life. The 12-sigma
    experimental cap matches the forecast's extreme-support guard.
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
