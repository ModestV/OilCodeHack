from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.forecast import forecast_sulfur


def _write_dataset(directory: Path, future_temperature: float) -> None:
    directory.mkdir(parents=True)
    rows = [
        {"metric_id": "ht.T6", "timestamp": "2025-01-01T03:50:00", "value": 300.0, "source": "kip", "flags": ""},
        # This row is after the four-hour feature cutoff for an 08:00 forecast.
        {"metric_id": "ht.T6", "timestamp": "2025-01-01T05:00:00", "value": future_temperature, "source": "kip", "flags": ""},
        {"metric_id": "lims.ht.2.Mg.Sulfur", "timestamp": "2024-12-31T20:00:00", "value": 8.0, "source": "lims", "flags": ""},
    ]
    table = pa.Table.from_pydict(
        {
            "metric_id": [row["metric_id"] for row in rows],
            "timestamp": pa.Array.from_pandas(pd.to_datetime([row["timestamp"] for row in rows])),
            "value": [row["value"] for row in rows],
            "source": [row["source"] for row in rows],
            "unit": [None] * len(rows),
            "flags": [row["flags"] for row in rows],
            "source_file": ["test.csv"] * len(rows),
            "source_row": list(range(1, len(rows) + 1)),
        }
    )
    pq.write_table(table, directory / "observations.parquet")


def test_runtime_forecast_uses_only_rows_before_availability_cutoff(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_dataset(first, future_temperature=100.0)
    _write_dataset(second, future_temperature=900.0)

    one = forecast_sulfur(first, "2025-01-01T08:00:00")
    two = forecast_sulfur(second, "2025-01-01T08:00:00")

    assert one["leakage_check"]["passed"] is True
    assert one["feature_time"] == "2025-01-01T03:50:00"
    assert one["feature_cutoff"] == "2025-01-01T04:00:00"
    assert one["prediction_ridge"] == two["prediction_ridge"]
    assert one["prediction_risk_guard"] == two["prediction_risk_guard"]


def test_forecast_endpoint_returns_model_evidence(tmp_path, monkeypatch):
    _write_dataset(tmp_path / "storage" / "dataset", future_temperature=900.0)
    (tmp_path / "storage" / "dataset" / "manifest.json").write_text(
        '{"id":"dataset","status":"ready"}', encoding="utf-8"
    )
    monkeypatch.setattr(app_module, "STORAGE", tmp_path / "storage")
    with TestClient(app_module.app) as client:
        response = client.get(
            "/api/datasets/dataset/forecast", params={"at": "2025-01-01T08:00:00"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["target"]["metric_id"] == "lims.ht.2.Mg.Sulfur"
    assert body["model"]["name"].startswith("standardized Ridge")
    assert body["leakage_check"]["passed"] is True
    assert np.isfinite(body["prediction_ridge"])
