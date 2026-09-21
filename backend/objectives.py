"""Declared experimental proxies; neither failure probabilities nor plant costs."""
from __future__ import annotations

from math import isfinite, log

from tools.modeling.sulfur_features import mahalanobis_score

SEVERITY_CLASSES = ((0.5, "normal"), (0.8, "elevated"))
PARETO_KEYS = ("exceedance_probability", "throughput_loss", "energy_cost_index", "risk_index", "effort")


def regime_severity(controls: dict, model_entry: dict | None) -> dict:
    """High-side loading of T6/P13/F9 relative to the training regime of the applicable model.

    No failure labels exist.  The index is the largest positive position of a
    control between the train median (0) and the upper edge of the train
    support (1) — the same edge at which the forecast itself refuses to score
    — so a proposed control vector cannot pass this gate at a regime the
    forecast would abstain on.  Classes: normal < 0.5 <= elevated < 0.8 <=
    high; above 1 the vector is outside the experimental model range.
    """
    if not model_entry:
        return {"status": "unavailable", "index": None, "class": None, "factors": [], "reason": "Артефакт модели прогноза недоступен"}
    horizon = max(model_entry["support"], key=int)
    support = model_entry["support"][horizon]
    columns = list(support["feature_columns"])
    factors = []
    for metric in ("ht.T6", "ht.P13", "ht.F9"):
        value = controls.get(metric)
        if value is None or not isfinite(value):
            return {"status": "unavailable", "index": None, "class": None, "factors": factors,
                    "reason": f"Нет достоверного значения {metric} для индекса нагрузки"}
        i = columns.index(metric.split(".")[1])
        median, upper = float(support["q50"][i]), float(support["support_upper"][i])
        position = (value - median) / (upper - median) if upper > median else 0.0
        factors.append({"metric_id": metric, "value": value, "train_median": median, "train_upper": upper,
                        "position": max(0.0, position)})
    index = max(f["position"] for f in factors)
    return {"status": "ok", "index": index, "class": _classify(index), "within_model_limit": index <= 1, "model_limit": 1.0,
            "factors": factors, "model_fit_end": model_entry.get("fit_end"),
            "basis": "max positive (value - train median) / (train upper support - train median) over T6, P13, F9; "
                     "1 = edge of the forecast's own applicability range",
            "failure_probability": None,
            "assumption": "Большие T6/P13/F9 условно повышают нагрузку. Граница 1 — край обучающего режима модели, экспериментальный, "
                          "не промышленный предел."}


def _classify(index: float) -> str:
    for edge, label in SEVERITY_CLASSES:
        if index < edge:
            return label
    return "high"


def anomaly_score(values: dict, model_entry: dict | None) -> dict:
    """Robust Mahalanobis index of the reactor-block vector against the applicable model's train regime."""
    if not model_entry or not model_entry.get("anomaly"):
        return {"status": "unavailable", "index": None, "class": None, "factors": [], "reason": "Модель аномалии режима недоступна"}
    return mahalanobis_score(model_entry["anomaly"], values)


def risk_assessment(controls: dict, values: dict, model_entry: dict | None) -> dict:
    """Reliability-agent risk index: the larger of regime severity and process anomaly.

    Classes: normal / elevated (attention) / high.  Contributing factors are
    the control positions and the top anomaly contributions.  A proxy with
    stated assumptions, not a probability of equipment failure.
    """
    severity = regime_severity(controls, model_entry)
    anomaly = anomaly_score(values, model_entry)
    components = []
    if severity["status"] == "ok":
        components.append(("severity", severity["index"], severity["class"]))
    if anomaly["status"] == "ok":
        anomaly_class = {"normal": "normal", "attention": "elevated", "high": "high"}[anomaly["class"]]
        components.append(("anomaly", anomaly["index"], anomaly_class))
    if not components:
        return {"status": "unavailable", "index": None, "class": None, "severity": severity, "anomaly": anomaly, "factors": [],
                "reason": severity.get("reason") or anomaly.get("reason")}
    order = {"normal": 0, "elevated": 1, "high": 2}
    dominant = max(components, key=lambda c: (order[c[2]], c[1]))
    factors = [{"kind": "control_position", **f} for f in severity.get("factors", [])]
    factors += [{"kind": "anomaly_contribution", **f} for f in anomaly.get("factors", [])[:3]]
    return {"status": "ok", "index": float(dominant[1]), "class": dominant[2], "dominant": dominant[0],
            "severity": severity, "anomaly": anomaly, "factors": factors,
            "basis": "max of regime severity (position within train support) and robust Mahalanobis anomaly (d²/q99 train)",
            "assumption": "Прокси тяжести режима без разметки отказов: не вероятность аварии.",
            "failure_probability": None}


def candidate_objectives(scenario: dict, effort: float, max_exceedance_probability: float = 0.3,
                         risk: dict | None = None, target_sulfur: float | None = None) -> dict:
    """Multi-criteria objectives of a candidate and the weighted ranking loss.

    ``ranking_loss`` (lower is better) trades throughput, an energy proxy,
    the reliability risk index, the size of the move, the residual P(>10)
    and an *overshoot* term (sulphur pushed below the editable target wastes
    energy/catalyst life without quality benefit).  Weights are explicit
    experimental preferences; infeasible candidates never enter the ranking.
    """
    controls = scenario["controls"]
    temperature = controls["ht.T6"]["change"]
    pressure = controls["ht.P13"]["change"]
    feed_pct = controls["ht.F9"]["change"]
    throughput = 1 + feed_pct / 100
    energy = 1 + .10 * temperature / 10 + .05 * pressure / 2 + .10 * feed_pct / 10
    # Off-spec product costs 50-100x the quality margin (organisers), so the
    # residual exceedance probability enters the ranking as an expected-cost
    # proxy.  This is a configured preference, never a permission to violate
    # quality: infeasible candidates are excluded before ranking.
    exceedance = scenario.get("exceedance_probability")
    quality_risk = (exceedance / max_exceedance_probability) if exceedance is not None and max_exceedance_probability > 0 else 0.0
    risk_index = (risk or {}).get("index")
    predicted = scenario.get("predicted_sulfur")
    overshoot = 0.0
    if target_sulfur and predicted and predicted > 0:
        overshoot = max(0.0, log(target_sulfur / predicted)) / log(1.25)
    loss = (0.5 * (1 - throughput) / .10 + .25 * (energy - 1) / .25
            + .25 * risk_index + .15 * effort + .5 * quality_risk + .25 * overshoot) if risk_index is not None else None
    return {"throughput_index": throughput, "throughput_loss": 1 - throughput, "throughput_change_pct": feed_pct,
            "energy_cost_index": energy, "risk_index": risk_index, "risk_class": (risk or {}).get("class"),
            "regime_severity": (risk or {}).get("severity") or {"status": "unavailable", "index": None, "failure_probability": None},
            "exceedance_probability": exceedance, "quality_risk_index": quality_risk, "effort": effort,
            "overshoot_index": overshoot,
            "ranking_loss": loss,
            "ranking_formula": "0.5*(1-throughput)/0.10 + 0.25*(energy-1)/0.25 + 0.25*risk + 0.15*effort + 0.5*P(>10)/P_max "
                               "+ 0.25*max(0, ln(target/S))/ln(1.25)",
            "basis": "scenario assumptions; throughput assumes unchanged yield; energy has no monetary units; P(>10) from forecast residuals "
                     "widened by coefficient uncertainty; risk = reliability-agent index; overshoot = sulphur below the editable target",
            "energy_formula": "1 + 0.10*delta_T/10 + 0.05*delta_P/2 + 0.10*delta_feed_pct/10"}


def pareto_front(candidates: list[dict], keys: tuple[str, ...] = PARETO_KEYS) -> list[dict]:
    """Non-dominated candidates (all criteria minimised); each gets ``pareto`` / ``dominated_by``.

    A missing criterion is treated as 0 (not worse than any value) so that a
    candidate without a probability estimate is compared on the others.
    """

    def vector(item: dict) -> list[float]:
        objectives = item.get("objectives") or {}
        return [float(objectives.get(key) if objectives.get(key) is not None else 0.0) for key in keys]

    front = []
    for item in candidates:
        v = vector(item)
        dominators = [other["id"] for other in candidates if other is not item
                      and all(a <= b for a, b in zip(vector(other), v)) and any(a < b for a, b in zip(vector(other), v))]
        item["dominated_by"] = dominators
        item["pareto"] = not dominators
        if not dominators:
            front.append(item)
    return front
