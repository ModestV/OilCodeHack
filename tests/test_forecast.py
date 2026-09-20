"""Independent temporal counterexamples and offline/runtime feature parity."""
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.forecast import _runtime_features, forecast_sulfur
from tools.modeling.sulfur_features import telemetry_features


def dataset(directory, future=900, unpublished=500, missing=False):
    directory.mkdir(parents=True)
    rows = [] if missing else [
        ("ht.T6", "2025-01-01T07:00", 10., "kip"),
        ("ht.T6", "2025-01-01T07:10", 20., "kip"),
        ("ht.T6", "2025-01-01T08:00", 30., "kip"),
        ("ht.T6", "2025-01-01T08:10", future, "kip"),
    ]
    rows += [("lims.ht.2.Mg.Sulfur", "2025-01-01T04:00", 8., "lims"),
             ("lims.ht.2.Mg.Sulfur", "2025-01-01T04:06", unpublished, "lims")]
    frame = pd.DataFrame(rows, columns=["metric_id", "timestamp", "value", "source"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["flags"] = ""
    frame.to_parquet(directory / "observations.parquet", index=False)
    (directory / "manifest.json").write_text('{"id":"dataset","status":"ready"}')


def artifact(tmp_path):
    result = {"version": 2, "feature_columns": ["242000__T6", "242000__T6__mean_1h", "242000__T6__mean_6h", "previous_lab_available"],
              "medians": [20, 20, 20, 8], "mean": [20, 20, 20, 8], "scale": [10, 10, 10, 2],
              "coef": [2, .1, .1, .1, .1], "support_lower": [0, 0, 0, 0], "support_upper": [100, 100, 100, 100],
              "applicability_policy": {"max_missing_fraction": .2, "max_ood_fraction": .1, "max_absolute_z": 12},
              "forecast_horizon_minutes": 180, "lims_publication_delay_minutes": 240}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path, result


def test_future_telemetry_and_unpublished_lims_cannot_change_forecast(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "a", future=900, unpublished=500)
    dataset(tmp_path / "b", future=1, unpublished=1)
    one = forecast_sulfur(tmp_path / "a", "2025-01-01T08:05", path)
    two = forecast_sulfur(tmp_path / "b", "2025-01-01T08:05", path)
    assert one["status"] == "ok"
    assert one["prediction_ridge"] == two["prediction_ridge"]
    assert one["previous_lab"]["value"] == 8
    assert one["previous_lab"]["available_at"] == "2025-01-01T08:00:00"
    assert one["feature_time"] == "2025-01-01T08:00:00"
    assert one["feature_cutoff"] == "2025-01-01T08:05:00"
    assert one["target_time"] == "2025-01-01T11:05:00"
    assert one["leakage_check"]["passed"]


def test_lims_enters_exactly_at_publication_boundary(tmp_path):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", unpublished=9)
    before = forecast_sulfur(tmp_path / "data", "2025-01-01T08:05:59", path)
    after = forecast_sulfur(tmp_path / "data", "2025-01-01T08:06:00", path)
    assert before["previous_lab"]["value"] == 8
    assert after["previous_lab"]["value"] == 9


def test_historical_origin_cannot_use_model_selected_later(tmp_path):
    path, model = artifact(tmp_path)
    model["model_available_from"] = "2026-01-01T00:00:00"
    path.write_text(json.dumps(model), encoding="utf-8")
    dataset(tmp_path / "data")
    result = forecast_sulfur(tmp_path / "data", "2025-01-01T08:05", path)
    assert result["status"] == "abstain"
    assert "model_not_yet_available_at_origin" in result["reasons"]
    assert result["prediction_ridge"] is None


def test_off_grid_runtime_features_match_offline_and_manual_windows(tmp_path):
    _, model = artifact(tmp_path)
    dataset(tmp_path / "data")
    origin = pd.Timestamp("2025-01-01T08:05")
    raw, latest, previous = _runtime_features(tmp_path / "data", origin, model)
    np.testing.assert_allclose(raw, [30, 25, 20, 8])
    telemetry = pd.DataFrame({"242000__T6": [10., 20., 30., 900.]},
                             index=pd.to_datetime(["2025-01-01T07:00", "2025-01-01T07:10", "2025-01-01T08:00", "2025-01-01T08:10"]))
    offline, _ = telemetry_features(telemetry, pd.DatetimeIndex([origin]))
    np.testing.assert_allclose(raw[:3], offline.iloc[0].to_numpy())
    assert latest == pd.Timestamp("2025-01-01T08:00")
    assert previous["value"] == 8


@pytest.mark.parametrize("missing,at,reason", [
    (True, "2025-01-01T08:05", "no_telemetry_evidence"),
    (False, "2025-01-02T08:05", "too_many_missing_features"),
    (False, "2025-01-01T08:10", "outside_training_support"),
])
def test_unsupported_inputs_abstain_instead_of_median_forecast(tmp_path, missing, at, reason):
    path, _ = artifact(tmp_path)
    dataset(tmp_path / "data", missing=missing)
    result = forecast_sulfur(tmp_path / "data", at, path)
    assert result["status"] == "abstain"
    assert reason in result["reasons"]
    assert result["prediction_ridge"] is result["prediction_risk_guard"] is result["alarm_above_10"] is None


def test_feature_time_units_are_explicit():
    telemetry = pd.DataFrame({"x": [3.]}, index=pd.DatetimeIndex(["2025-01-01"]).as_unit("us"))
    frame, latest = telemetry_features(telemetry, pd.DatetimeIndex(["2025-01-01T00:05"]).as_unit("ns"))
    assert frame.iloc[0].tolist() == [3, 3, 3]
    assert latest.iloc[0] == pd.Timestamp("2025-01-01")


def test_forecast_endpoint_reports_missing_coverage(tmp_path, monkeypatch):
    dataset(tmp_path / "storage" / "dataset", missing=True)
    monkeypatch.setattr(app_module, "STORAGE", tmp_path / "storage")
    with TestClient(app_module.app) as client:
        response = client.get("/api/datasets/dataset/forecast", params={"at": "2025-01-01T08:05"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "abstain"
    assert body["target"]["metric_id"] == "lims.ht.2.Mg.Sulfur"
    assert body["leakage_check"]["passed"]
    assert body["prediction_ridge"] is None
