from fastapi.testclient import TestClient

from backend import app as module
from backend.agents import AgentContext, QualityAgent
from backend.scenarios import select_sulfur


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORAGE", tmp_path / "storage")
    return TestClient(module.app)


def test_sulfur_source_priority_prefers_lims_then_pak_then_kip():
    values = {
        "lims.ht.2.Mg.Sulfur": {"value": 8.2},
        "pak.ht.Mg.Sulfur": {"value": 6.1},
        "ht.Q21": {"value": 5.4},
    }
    assert select_sulfur(values) == (8.2, "lims.ht.2.Mg.Sulfur")

    values.pop("lims.ht.2.Mg.Sulfur")
    assert select_sulfur(values) == (6.1, "pak.ht.Mg.Sulfur")

    values.pop("pak.ht.Mg.Sulfur")
    assert select_sulfur(values) == (5.4, "ht.Q21")


def test_quality_agent_exposes_the_selected_lab_source(tmp_path):
    frame = {
        "values": [
            {
                "metric_id": "lims.ht.2.Mg.Sulfur",
                "value": 8.2,
                "freshness": "fresh",
                "flags": [],
                "timestamp": "2025-01-01T00:00:00",
            },
            {
                "metric_id": "pak.ht.Mg.Sulfur",
                "value": 6.1,
                "freshness": "fresh",
                "flags": [],
                "timestamp": "2025-01-01T00:00:00",
            },
            *[
                {
                    "metric_id": metric_id,
                    "value": value,
                    "freshness": "fresh",
                    "flags": [],
                    "timestamp": "2025-01-01T00:00:00",
                }
                for metric_id, value in (("ht.T6", 300), ("ht.F9", 100), ("ht.P13", 5))
            ],
        ]
    }
    context = AgentContext(
        directory=tmp_path,
        request=module.ScenarioRequest(at="2025-01-01T00:00:00"),
        frame=frame,
    )
    quality = QualityAgent().run(context)
    assert quality["evidence"]["sulfur"]["source"] == "lims.ht.2.Mg.Sulfur"
    assert quality["evidence"]["sulfur"]["value"] == 8.2


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
            json={"at": "2025-01-01T00:00:00", "current_sulfur": 25},
        )

    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "abstain"
    assert [item["role"] for item in body["trace"]] == [
        "quality",
        "reliability",
        "optimization",
    ]
    assert all("summary" in item and "status" in item for item in body["trace"])
    # An unreachable target is diagnostic evidence, never an action suggestion.
    assert body["recommendation"] is None
    assert body["scenario"] is None
    assert body["candidates"][0]["scenario"]["baseline"]["sulfur"] == 25
    assert [candidate["id"] for candidate in body["candidates"]] == [
        "automatic",
        "conservative",
        "hold",
    ]
    assert body["selected_candidate"] is None
    assert body["safety_gate"]["passed"] is False
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


def test_decision_abstains_when_quality_exists_but_controls_are_missing(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        uploaded = client.post(
            "/api/datasets",
            files=[
                (
                    "files",
                    (
                        "242000_tags.csv",
                        b"date,T1\n2025-01-01 00:00:00,1\n",
                        "text/csv",
                    ),
                )
            ],
        )
        dataset_id = uploaded.json()["id"]
        assert client.get(f"/api/datasets/{dataset_id}").json()["status"] == "ready"
        result = client.post(
            f"/api/datasets/{dataset_id}/decision",
            json={"at": "2025-01-01T00:00:00", "current_sulfur": 8},
        )

    body = result.json()
    assert body["status"] == "abstain"
    assert body["agents"]["quality"]["evidence"]["available_control_count"] == 0
    assert body["agents"]["reliability"]["confidence"] < 0.5
    assert body["recommendation"] is None


def test_decision_abstains_when_control_vector_is_partial(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        uploaded = client.post(
            "/api/datasets",
            files=[
                (
                    "files",
                    (
                        "242000_tags.csv",
                        b"date,T6,F9\n2025-01-01 00:00:00,300,100\n",
                        "text/csv",
                    ),
                )
            ],
        )
        dataset_id = uploaded.json()["id"]
        result = client.post(
            f"/api/datasets/{dataset_id}/decision",
            json={"at": "2025-01-01T00:00:00", "current_sulfur": 8},
        )

    body = result.json()
    assert body["status"] == "abstain"
    assert body["agents"]["quality"]["evidence"]["available_control_count"] == 2
    assert body["agents"]["reliability"]["confidence"] < 0.5
