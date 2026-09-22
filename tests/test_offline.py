"""The service runs with the network cut: import, forecast, agents and explanation are local.

Only the optional LLM explainer may open a connection, and it is off unless
``OILCODE_LLM_BASE_URL`` and ``OILCODE_LLM_MODEL`` are set.
"""

import socket

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as module


@pytest.fixture
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.delenv("OILCODE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OILCODE_LLM_MODEL", raising=False)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_upload_import_and_decision_without_network(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(module, "STORAGE", tmp_path / "storage")
    times = pd.date_range("2026-03-01", periods=6 * 24 * 3, freq="10min")
    csv = pd.DataFrame({"date": times, "T6": 360.0, "F9": 220.0, "P13": 3.9, "Q21": 8.0}).to_csv(index=False)
    with TestClient(module.app) as client:
        assert client.get("/api/health").json()["llm"]["enabled"] is False
        uploaded = client.post("/api/datasets", files=[("files", ("242000_tags.csv", csv.encode(), "text/csv"))],
                               data={"name": "offline"})
        assert uploaded.status_code == 202
        identifier = uploaded.json()["id"]
        assert client.get(f"/api/datasets/{identifier}").json()["status"] == "ready"
        response = client.post(f"/api/datasets/{identifier}/decision", json={"at": "2026-03-03T12:00:00"})
    assert response.status_code == 200
    decision = response.json()
    # No lab history: the forecast cannot anchor, so the contour must refuse and say why.
    assert decision["status"] == "abstain"
    assert decision["abstain"]["reason"]
    assert decision["explanation"]["source"] == "template"
    assert [step["role"] for step in decision["trace"]] == ["quality", "reliability", "optimization", "orchestrator"]
