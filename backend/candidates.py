"""Deterministic candidate grid of control moves for the optimisation agent.

The grid spans single-control steps and a few pairs inside the prototype
step limits (±10 °C, ±10 % feed, ±0.5 MPa), plus the solver's ``automatic``
vector and the operator's ``requested`` vector.  Candidates outside the
reliability constraints are still evaluated (as diagnostic evidence) but
are marked infeasible with the constraint that excludes them.
"""
from __future__ import annotations

from .scenarios import ControlChanges

TEMPERATURE_STEPS = (1.0, 2.0, 3.0, 5.0, 7.0, 10.0, -1.0, -2.0, -3.0, -5.0)
PRESSURE_STEPS = (0.1, 0.2, 0.3, 0.5, -0.1, -0.2)
FEED_STEPS = (-2.0, -5.0, -10.0, 2.0, 5.0)
PAIRS = (
    {"temperature": 2.0, "pressure": 0.1}, {"temperature": 3.0, "pressure": 0.1}, {"temperature": 3.0, "pressure": 0.2},
    {"temperature": 5.0, "pressure": 0.2}, {"temperature": 5.0, "pressure": 0.3},
    {"temperature": 3.0, "feed_rate_pct": -5.0}, {"temperature": 5.0, "feed_rate_pct": -5.0}, {"temperature": 5.0, "feed_rate_pct": -10.0},
    {"temperature": -2.0, "feed_rate_pct": 2.0}, {"temperature": -3.0, "feed_rate_pct": 5.0},
    {"pressure": 0.2, "feed_rate_pct": -5.0}, {"pressure": 0.3, "feed_rate_pct": -10.0},
)
CONTROL_LABELS = {"temperature": ("T6", "°C"), "feed_rate_pct": ("F9", "%"), "pressure": ("P13", "МПа")}


def _id(changes: ControlChanges) -> str:
    parts = []
    for field, (tag, _) in CONTROL_LABELS.items():
        value = getattr(changes, field)
        if value:
            parts.append(f"{tag.lower()}_{'p' if value > 0 else 'm'}{abs(value):g}")
    return "_".join(parts) or "hold"


def label(changes: ControlChanges) -> str:
    parts = [f"{tag} {round(getattr(changes, field), 2):+g} {unit}" for field, (tag, unit) in CONTROL_LABELS.items() if getattr(changes, field)]
    return ", ".join(parts) if parts else "Удержать текущий режим"


def _violations(changes: ControlChanges, constraints: dict | None, current: dict[str, float | None]) -> list[str]:
    """Constraint violations of a candidate (empty when admissible)."""
    if not constraints:
        return []
    reasons = []
    for field, (tag, unit) in CONTROL_LABELS.items():
        change = getattr(changes, field)
        rule = constraints.get(f"ht.{tag}") or {}
        if not change:
            continue
        if rule.get("blocked"):
            reasons.append(f"{tag}: изменение заблокировано агентом надёжности ({rule.get('reason') or 'ограничение'})")
            continue
        limit = rule.get("max_step_up") if change > 0 else rule.get("max_step_down")
        if limit is not None and abs(change) > limit + 1e-9:
            reasons.append(f"{tag}: шаг {change:+g} {unit} превышает допустимый {limit:g} {unit} (агент надёжности)")
        value = current.get(tag)
        if value is not None:
            target = value * (1 + change / 100) if field == "feed_rate_pct" else value + change
            if rule.get("min") is not None and target < rule["min"] - 1e-9:
                reasons.append(f"{tag}: значение {target:.2f} ниже допустимого диапазона {rule['min']:.2f}")
            if rule.get("max") is not None and target > rule["max"] + 1e-9:
                reasons.append(f"{tag}: значение {target:.2f} выше допустимого диапазона {rule['max']:.2f}")
    return reasons


def candidate_grid(automatic: ControlChanges | None, requested: ControlChanges | None,
                   constraints: dict | None = None, current: dict[str, float | None] | None = None) -> list[dict]:
    """Ordered, de-duplicated candidates: ``{"id", "label", "changes", "origin", "constraint_violations"}``."""
    current = current or {}
    definitions: list[tuple[str, ControlChanges, str]] = [("hold", ControlChanges(), "grid")]
    for step in TEMPERATURE_STEPS:
        definitions.append((None, ControlChanges(temperature=step), "grid"))
    for step in PRESSURE_STEPS:
        definitions.append((None, ControlChanges(pressure=step), "grid"))
    for step in FEED_STEPS:
        definitions.append((None, ControlChanges(feed_rate_pct=step), "grid"))
    for pair in PAIRS:
        definitions.append((None, ControlChanges(**pair), "grid"))
    if automatic is not None:
        definitions.append(("automatic", automatic, "solver"))
    if requested is not None:
        definitions.append(("requested", requested, "request"))
    seen: dict[str, dict] = {}
    ordered = []
    for candidate_id, changes, origin in definitions:
        key = _id(changes)
        candidate_id = candidate_id or key
        if candidate_id in ("automatic", "requested"):
            pass
        elif key in seen:
            continue
        item = {"id": candidate_id, "label": label(changes) if candidate_id not in ("automatic", "requested") else
                ("Изменение до цели в рамках модели" if candidate_id == "automatic" else "Изменение, заданное пользователем")
                + (f" ({label(changes)})" if key != "hold" else " (без изменений)"),
                "changes": changes, "origin": origin,
                "constraint_violations": _violations(changes, constraints, current)}
        seen.setdefault(key, item)
        ordered.append(item)
    return ordered
