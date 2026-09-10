import pytest
from fastapi.testclient import TestClient

from backend import app as module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORAGE", tmp_path / "storage")
    with TestClient(module.app) as client:
        yield client


def test_partial_csv_import_queries_export_and_settings(client):
    response = client.post(
        "/api/datasets",
        files=[
            (
                "files",
                (
                    "avt_tags.csv",
                    b"date,T1\n2025-01-01 00:00:00,100\n2025-01-01 00:10:00,110\n",
                    "text/csv",
                ),
            )
        ],
        data={"name": "Test"},
    )
    assert response.status_code == 202
    identifier = response.json()["id"]
    base = f"/api/datasets/{identifier}"
    assert client.get(base).json()["status"] == "ready"
    assert client.get("/api/datasets").json()["default_id"] == identifier
    assert client.get(base + "/metrics").json()["metrics"][0]["id"] == "avt.T1"
    snap = client.get(base + "/snapshot", params={"at": "2025-01-01T00:03:00"}).json()
    assert snap["values"][0]["value"] == 100 and snap["values"][0]["age_minutes"] == 3
    params = {"from": "2025-01-01T00:00:00", "to": "2025-01-01T01:00:00"}
    summary = client.get(base + "/summary", params=params)
    assert summary.status_code == 200 and summary.json()["metrics"][0]["median"] == 105
    series = client.get(base + "/series", params={**params, "metrics": "avt.T1"})
    assert series.status_code == 200 and len(series.json()["series"][0]["points"]) == 2
    export = client.get(base + "/export", params={**params, "metrics": "avt.T1"})
    assert export.status_code == 200 and "100.0" in export.text and "source_row" in export.text
    assert (
        client.put(
            base + "/settings",
            json={"freshness_minutes": {"kip": 1, "pak": 30, "lims": 2880}},
        ).status_code
        == 200
    )
    assert (
        client.get(base + "/snapshot", params={"at": "2025-01-01T00:03:00"}).json()["values"][0][
            "freshness"
        ]
        == "stale"
    )
    assert (
        client.get(base + "/series", params={**params, "metrics": "missing')"}).status_code == 422
    )
    assert (
        client.get(
            base + "/summary", params={"from": params["to"], "to": params["from"]}
        ).status_code
        == 422
    )


def test_upload_rejects_wrong_files_and_duplicate_names(client):
    assert (
        client.post("/api/datasets", files=[("files", ("data.rar", b"invalid"))]).status_code == 422
    )
    files = [
        ("files", ("same.csv", b"date,T1\n")),
        ("files", ("same.csv", b"date,T1\n")),
    ]
    assert client.post("/api/datasets", files=files).status_code == 422
    assert client.get("/api/datasets/no-such-id").status_code == 404


def test_bad_import_finishes_with_error_not_forever_loading(client):
    response = client.post("/api/datasets", files=[("files", ("broken.xlsx", b"not excel"))])
    result = client.get("/api/datasets/" + response.json()["id"]).json()
    assert result["status"] == "error"
