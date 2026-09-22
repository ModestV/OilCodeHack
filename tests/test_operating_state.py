"""Shutdown/start-up detection: the contour refuses to advise outside normal operation."""

from test_decision_matrix import decide, frame

from backend.objectives import operating_state


def test_normal_feed_is_normal_operation():
    state = operating_state({"ht.F9": 210.0})
    assert state["state"] == "normal"
    assert state["feed_fraction_of_train_mean"] > 0.9


def test_near_zero_feed_is_shutdown():
    assert operating_state({"ht.F9": -0.3})["state"] == "shutdown"
    assert operating_state({"ht.F9": 15.0})["state"] == "shutdown"


def test_partial_feed_is_transition():
    state = operating_state({"ht.F9": 95.0})
    assert state["state"] == "transition"


def test_missing_feed_is_unknown():
    assert operating_state({"ht.F9": None})["state"] == "unknown"


def test_shutdown_abstains_with_explicit_reason(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame(ht__F9={"value": 0.2}))
    assert decision["status"] == "abstain"
    assert decision["agents"]["reliability"]["operating_state"]["state"] == "shutdown"
    assert "останов" in decision["abstain"]["reason"]


def test_transition_abstains_even_when_all_signals_are_fresh(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame(ht__F9={"value": 90.0}))
    assert decision["status"] == "abstain"
    assert "пуск" in decision["abstain"]["reason"]


def test_normal_operation_is_not_blocked(monkeypatch, tmp_path):
    decision = decide(monkeypatch, tmp_path, frame())
    assert decision["agents"]["reliability"]["operating_state"]["state"] == "normal"
    assert decision["status"] == "recommendation"
