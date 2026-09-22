from copy import deepcopy

import pytest

from backend.objectives import annotate_pareto


def candidate(key, throughput=1., energy=1., severity=.2, cost=1., effort=0., feasible=True):
    return {"id": key, "feasible": feasible, "safety_gate": {"passed": feasible}, "effort": effort,
            "objectives": {"throughput_index": throughput, "energy_cost_index": energy,
                           "regime_severity": {"index": severity}, "blend_cost_index": cost}}


def test_front_keeps_tradeoffs_and_equal_points_without_changing_order_or_gates():
    items = [candidate("base"), candidate("equal"), candidate("worse", energy=2),
             candidate("tradeoff", throughput=1.1, energy=1.2), candidate("unsafe", energy=0, feasible=False)]
    before = deepcopy(items)
    annotate_pareto(items)
    assert [i["id"] for i in items] == [i["id"] for i in before]
    assert [i["pareto"] for i in items] == [True, True, False, True, None]
    assert items[2]["dominated_by"] == ["base", "equal", "tradeoff"]
    for current, original in zip(items, before):
        assert {k: current[k] for k in original} == original


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True])
def test_missing_objective_cannot_become_zero_risk_winner(missing):
    items = [candidate("known"), candidate("unknown", severity=missing)]
    annotate_pareto(items)
    assert items[0]["pareto"] is True
    assert items[1]["pareto"] is None
    assert items[1]["pareto_status"] == "incomplete_objectives"


def test_gate_failure_cannot_appear_on_front_even_if_feasible_flag_disagrees():
    item = candidate("inconsistent")
    item["safety_gate"]["passed"] = False
    annotate_pareto([item])
    assert item["pareto"] is None
