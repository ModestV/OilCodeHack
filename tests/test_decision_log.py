"""Audit log: every decision is stored with its request, trace and code revision."""

import json

import pytest
from fastapi.testclient import TestClient

from backend import app as module
from backend import decision_log

DECISION = {"at": "2026-01-08T07:00:00", "status": "recommendation", "selected_candidate": "hold",
            "trace": [{"step": 4, "role": "orchestrator"}], "explanation": {"source": "template", "text": "…"}}


def test_record_writes_request_decision_and_revision(tmp_path):
    record_id = decision_log.record(tmp_path, {"at": DECISION["at"]}, DECISION)
    stored = json.loads((tmp_path / "decisions" / f"{record_id}.json").read_text(encoding="utf-8"))
    assert stored["id"] == record_id
    assert stored["request"] == {"at": DECISION["at"]}
    assert stored["decision"] == DECISION
    assert stored["revision"]
    assert stored["created_at"]


def test_list_is_newest_first_with_summaries(tmp_path):
    first = decision_log.record(tmp_path, {}, DECISION)
    second = decision_log.record(tmp_path, {}, {**DECISION, "status": "abstain", "selected_candidate": None})
    items = decision_log.list_records(tmp_path)
    assert [item["id"] for item in items] == [second, first]
    assert items[0] == {**items[0], "status": "abstain", "at": DECISION["at"], "explanation_source": "template"}


def test_list_limit(tmp_path):
    for _ in range(3):
        decision_log.record(tmp_path, {}, DECISION)
    assert len(decision_log.list_records(tmp_path, limit=2)) == 2


@pytest.mark.parametrize("bad", ["../manifest", "x/y", "", "a" * 200])
def test_get_rejects_path_traversal_and_bad_ids(tmp_path, bad):
    with pytest.raises(KeyError):
        decision_log.get_record(tmp_path, bad)


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OILCODE_DECISION_LOG", "0")
    assert decision_log.record(tmp_path, {}, DECISION) is None
    assert not (tmp_path / "decisions").exists()


def test_api_records_and_serves_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORAGE", tmp_path)
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({"id": "demo", "status": "ready"}), encoding="utf-8")
    monkeypatch.setattr(module, "make_decision", lambda *_args: dict(DECISION))
    client = TestClient(module.app)
    body = client.post("/api/datasets/demo/decision", json={"at": DECISION["at"]}).json()
    assert body["record_id"]
    listed = client.get("/api/datasets/demo/decisions").json()
    assert [item["id"] for item in listed] == [body["record_id"]]
    stored = client.get(f"/api/datasets/demo/decisions/{body['record_id']}").json()
    assert stored["decision"]["status"] == "recommendation"
    assert stored["request"]["at"] == DECISION["at"]
    assert client.get("/api/datasets/demo/decisions/nope").status_code == 404


def test_log_failure_does_not_break_the_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORAGE", tmp_path)
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({"id": "demo", "status": "ready"}), encoding="utf-8")
    (directory / "decisions").write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(module, "make_decision", lambda *_args: dict(DECISION))
    response = TestClient(module.app).post("/api/datasets/demo/decision", json={"at": DECISION["at"]})
    assert response.status_code == 200
    assert response.json()["status"] == "recommendation"
    assert response.json()["record_id"] is None
