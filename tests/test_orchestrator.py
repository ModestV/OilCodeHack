"""Orchestrator conflict resolution, consistency checks and the template explanation."""

from backend.agents import Orchestrator
from backend.explain import build_explanation
from backend.scenarios import ScenarioRequest

AT = "2026-01-08T07:00:00"


def _check(name, passed=True):
    return {"name": name, "passed": passed, "basis": "test"}


def _candidate(candidate_id, *, feasible, loss, target_met=True, sulfur=8.0, checks=None,
               reasons=None, controls=None, at=AT, shares=None):
    checks = checks if checks is not None else [_check("sulfur_hard_limit"), _check("regime_severity_model_limit")]
    controls = controls or {
        "ht.T6": {"current": 360.0, "change": 0, "recommended": 360.0, "relative": False},
        "ht.F9": {"current": 100.0, "change": 0, "recommended": 100.0, "relative": True},
        "ht.P13": {"current": 3.9, "change": 0, "recommended": 3.9, "relative": False},
    }
    recipe = {"tanks": [{"name": f"R{i}", "share": s} for i, s in enumerate(shares)]} if shares else None
    return {
        "id": candidate_id,
        "label": candidate_id,
        "status": "ok",
        "feasible": feasible,
        "reason": None if feasible else "; ".join(reasons or ["не прошёл"]),
        "predicted_sulfur": sulfur,
        "product_sulfur": sulfur,
        "target_met": target_met,
        "objectives": {"ranking_loss": loss, "throughput_index": 1.0, "energy_cost_index": 1.0,
                       "regime_severity": {"status": "ok", "index": 0.1, "class": "normal"}},
        "controls": controls,
        "safety_gate": {"passed": feasible, "reasons": reasons or [], "checks": checks},
        "scenario": {"at": at, "product_sulfur": sulfur, "applied_recipe": recipe, "blend": None},
    }


def _roles(candidates, selected=None, *, alarm=False, analyser_conflict=False, basis="observed_and_forecast",
           can_recommend=True, optimize_economics=False):
    quality = {
        "role": "quality", "status": "ok", "summary": "ok", "missing": [], "warnings": [],
        "evidence": {
            "sulfur": {"source": "model.nowcast", "value": 8.6},
            "controls": {},
            "model_forecast": {"status": "ok", "at": AT, "feature_time": AT, "prediction": 8.4,
                               "prediction_lower": 6.2, "prediction_upper": 10.1, "exceedance_probability": 0.12,
                               "horizon_minutes": 180, "alarm_above_10": alarm, "leakage_check": {"passed": True}},
            "analyzer_comparison": {"conflict": analyser_conflict, "difference_mgkg": 3.0 if analyser_conflict else 0.5},
        },
    }
    reliability = {"role": "reliability", "status": "ok" if can_recommend else "insufficient",
                   "summary": "ok", "can_recommend": can_recommend, "basis": basis, "reasons": [], "warnings": []}
    chosen = next((c for c in candidates if c["id"] == selected), None)
    optimization = {"role": "optimization", "status": "ok" if chosen else "insufficient", "summary": "ok",
                    "candidates": candidates, "selected_candidate": selected,
                    "safety_gate": chosen["safety_gate"] if chosen else {"passed": False, "reasons": [], "checks": []}}
    if chosen:
        optimization["recommendation"] = {"candidate_id": selected, "controls": chosen["controls"],
                                          "predicted_sulfur": chosen["product_sulfur"], "objectives": chosen["objectives"],
                                          "target_sulfur": 9.0, "target_met": True}
        optimization["scenario"] = chosen["scenario"]
    request = ScenarioRequest(at=AT, optimize_economics=optimize_economics)
    return request, quality, reliability, optimization


def test_reliability_limit_blocking_the_only_target_reaching_candidate_is_a_quality_vs_reliability_conflict():
    blocked = _candidate("automatic", feasible=False, loss=0.1,
                         checks=[_check("sulfur_hard_limit"), _check("regime_severity_model_limit", False)],
                         reasons=["Индекс нагрузки недоступен или превышает экспериментальный предел"])
    hold = _candidate("hold", feasible=False, loss=0.2, target_met=False, sulfur=10.5,
                      checks=[_check("sulfur_hard_limit", False)], reasons=["Сера превышает 10"])
    request, quality, reliability, optimization = _roles([blocked, hold])
    conflicts = Orchestrator._conflicts(request, quality, reliability, optimization)
    codes = {c["code"]: c for c in conflicts}
    assert codes["quality_vs_reliability"]["resolution"] == "abstain"
    assert "automatic" in codes["quality_vs_reliability"]["message"]


def test_reliability_conflict_is_resolved_when_another_candidate_is_admissible():
    blocked = _candidate("automatic", feasible=False, loss=0.1,
                         checks=[_check("sulfur_hard_limit"), _check("regime_severity_model_limit", False)])
    ok = _candidate("conservative", feasible=True, loss=0.2)
    request, quality, reliability, optimization = _roles([ok, blocked], "conservative")
    codes = {c["code"]: c for c in Orchestrator._conflicts(request, quality, reliability, optimization)}
    assert codes["quality_vs_reliability"]["resolution"] == "resolved_by_alternative"


def test_economic_candidates_rejected_by_quality_are_resolved_in_favour_of_quality():
    rejected = _candidate("lower_heat", feasible=False, loss=0.05, target_met=False, sulfur=10.4,
                          checks=[_check("sulfur_hard_limit", False)], reasons=["Сера превышает 10"])
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, reliability, optimization = _roles([hold, rejected], "hold", optimize_economics=True)
    codes = {c["code"]: c for c in Orchestrator._conflicts(request, quality, reliability, optimization)}
    assert codes["economy_vs_quality"]["resolution"] == "quality_priority"
    assert "lower_heat" in codes["economy_vs_quality"]["message"]


def test_hold_kept_despite_marginally_better_candidate_is_recorded():
    hold = _candidate("hold", feasible=True, loss=0.30)
    better = _candidate("more_feed", feasible=True, loss=0.29)
    request, quality, reliability, optimization = _roles([hold, better], "hold")
    codes = {c["code"]: c for c in Orchestrator._conflicts(request, quality, reliability, optimization)}
    assert codes["stability_vs_economy"]["resolution"] == "hold"


def test_analyser_disagreement_is_a_data_source_conflict():
    hold = _candidate("hold", feasible=False, loss=0.3)
    request, quality, reliability, optimization = _roles([hold], analyser_conflict=True, can_recommend=False)
    codes = {c["code"]: c for c in Orchestrator._conflicts(request, quality, reliability, optimization)}
    assert codes["analyser_disagreement"]["resolution"] == "abstain"


def test_no_conflicts_in_a_plain_admissible_hold():
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, reliability, optimization = _roles([hold], "hold")
    assert Orchestrator._conflicts(request, quality, reliability, optimization) == []


def test_consistency_passes_for_a_coherent_decision():
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, _, optimization = _roles([hold], "hold")
    checks = Orchestrator._consistency(request, quality, optimization)
    assert checks and all(c["passed"] for c in checks), checks


def test_consistency_fails_when_selected_candidate_breaks_the_hard_limit():
    bad = _candidate("hold", feasible=True, loss=0.3, sulfur=10.2)
    request, quality, _, optimization = _roles([bad], "hold")
    failed = {c["code"] for c in Orchestrator._consistency(request, quality, optimization) if not c["passed"]}
    assert "selected_within_hard_limits" in failed


def test_consistency_fails_when_blend_shares_do_not_sum_to_100():
    bad = _candidate("hold", feasible=True, loss=0.3, shares=[60, 30])
    request, quality, _, optimization = _roles([bad], "hold")
    failed = {c["code"] for c in Orchestrator._consistency(request, quality, optimization) if not c["passed"]}
    assert "blend_shares_sum_100" in failed


def test_consistency_fails_when_a_candidate_was_calculated_for_another_moment():
    hold = _candidate("hold", feasible=True, loss=0.3)
    other = _candidate("automatic", feasible=True, loss=0.4, at="2026-01-08T08:00:00")
    request, quality, _, optimization = _roles([hold, other], "hold")
    failed = {c["code"] for c in Orchestrator._consistency(request, quality, optimization) if not c["passed"]}
    assert "same_origin" in failed


def test_consistency_fails_when_forecast_features_are_after_the_decision_moment():
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, _, optimization = _roles([hold], "hold")
    quality["evidence"]["model_forecast"]["feature_time"] = "2026-01-08T07:10:00"
    failed = {c["code"] for c in Orchestrator._consistency(request, quality, optimization) if not c["passed"]}
    assert "same_origin" in failed


def test_resolve_turns_an_abstain_conflict_into_a_refusal_and_overrides_the_gate():
    blocked = _candidate("automatic", feasible=False, loss=0.1,
                         checks=[_check("sulfur_hard_limit"), _check("regime_severity_model_limit", False)])
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, reliability, optimization = _roles([hold, blocked], "hold")
    conflicts = [{"code": "x", "resolution": "abstain", "message": "Тестовый конфликт"}]
    decision = Orchestrator._assemble(request, quality, reliability, optimization, conflicts, [])
    assert decision["status"] == "abstain"
    assert decision["recommendation"] is None
    assert decision["safety_gate"]["passed"] is False
    assert decision["safety_gate"]["orchestrator_override"] is True
    assert "Тестовый конфликт" in decision["abstain"]["reason"]


def test_trace_has_four_roles_with_data_flow():
    hold = _candidate("hold", feasible=True, loss=0.3)
    request, quality, reliability, optimization = _roles([hold], "hold")
    decision = Orchestrator._assemble(request, quality, reliability, optimization, [], [])
    assert [step["role"] for step in decision["trace"]] == ["quality", "reliability", "optimization", "orchestrator"]
    assert all(step["consumes"] and step["produces"] for step in decision["trace"])


def test_template_explanation_for_a_recommendation_names_action_effect_and_checks():
    change = {
        "ht.T6": {"current": 360.0, "change": 2.0, "recommended": 362.0, "relative": False},
        "ht.F9": {"current": 100.0, "change": 0, "recommended": 100.0, "relative": True},
        "ht.P13": {"current": 3.9, "change": 0, "recommended": 3.9, "relative": False},
    }
    chosen = _candidate("automatic", feasible=True, loss=0.2, sulfur=8.1, controls=change)
    alt = _candidate("hold", feasible=True, loss=0.3, sulfur=9.4)
    request, quality, reliability, optimization = _roles([chosen, alt], "automatic")
    decision = Orchestrator._assemble(request, quality, reliability, optimization, [], [])
    explanation = build_explanation(decision)
    assert explanation["source"] == "template"
    assert "360.0 → 362.0" in explanation["text"]
    assert "8.1" in explanation["expected_effect"]
    assert "sulfur_hard_limit" in explanation["constraints_check"]
    assert explanation["alternatives"] == [{"id": "hold", "label": "hold"}]
    assert "проверки технологом" in explanation["text"]


def test_template_explanation_for_abstain_gives_reason_and_no_action():
    hold = _candidate("hold", feasible=False, loss=0.3, reasons=["Сера превышает 10"])
    request, quality, reliability, optimization = _roles([hold], alarm=True)
    decision = Orchestrator._assemble(request, quality, reliability, optimization, [], [])
    explanation = build_explanation(decision)
    assert decision["status"] == "abstain"
    assert explanation["action"] == "Рекомендация не выдана"
    assert "Надёжной рекомендации нет" in explanation["text"]
    assert explanation["alternatives"] == []
