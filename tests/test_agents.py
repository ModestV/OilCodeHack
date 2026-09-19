from fastapi.testclient import TestClient

from backend import app as module


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORAGE", tmp_path / "storage")
    return TestClient(module.app)


def test_decision_runs_deterministic_agents_and_returns_trace(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        uploaded = client.post(
            "/api/datasets",
            files=[
                (
                    "files",
                    (
                        "242000_tags.csv",
                        b"date,T6,F9,P13\n2025-01-01 00:00:00,300,100,5\n",
                        "text/csv",
                    ),
                )
            ],
        )
        dataset_id = uploaded.json()["id"]
        assert client.get(f"/api/datasets/{dataset_id}").json()["status"] == "ready"
        result = client.post(
            f"/api/datasets/{dataset_id}/decision",
            json={"at": "2025-01-01T00:00:00", "current_sulfur": 12},
        )

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "recommendation"
    assert [item["role"] for item in body["trace"]] == [
        "quality",
        "reliability",
        "optimization",
    ]
    assert all("summary" in item and "status" in item for item in body["trace"])
    # The deterministic model still returns a reviewable recommendation when
    # its editable bounds cannot reach the target; the action is then escalate.
    assert body["recommendation"]["target_met"] is False
    assert body["recommendation"]["action"] == "escalate"
    assert body["scenario"]["baseline"]["sulfur"] == 12
    assert "внешний LLM" in body["assumptions"][0]


def test_decision_abstains_when_sulfur_baseline_is_missing(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        uploaded = client.post(
            "/api/datasets",
            files=[
                (
                    "files",
                    (
                        "242000_tags.csv",
                        b"date,T6\n2025-01-01 00:00:00,300\n",
                        "text/csv",
                    ),
                )
            ],
        )
        dataset_id = uploaded.json()["id"]
        assert client.get(f"/api/datasets/{dataset_id}").json()["status"] == "ready"
        result = client.post(
            f"/api/datasets/{dataset_id}/decision",
            json={"at": "2025-01-01T00:00:00"},
        )

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "abstain"
    assert body["recommendation"] is None
    assert body["scenario"] is None
    assert body["abstain"]["missing"] == ["sulfur_baseline"]
    assert body["agents"]["optimization"]["status"] == "skipped"
    assert body["trace"][-1]["status"] == "skipped"
