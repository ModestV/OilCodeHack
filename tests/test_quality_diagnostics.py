import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend.quality_diagnostics import METRICS, UNITS, quality_diagnostics

AT = "2026-07-21T10:00:00"


def dataset(path, rows):
    path.mkdir(exist_ok=True)
    records = []
    for name, ts, value, *extra in rows:
        records.append(dict(metric_id=METRICS[name], timestamp=pd.Timestamp(ts), value=value,
                            flags=extra[0] if extra else "", unit=extra[1] if len(extra) > 1 else UNITS[name]))
    pd.DataFrame(records).to_parquet(path / "observations.parquet", index=False)
    (path / "manifest.json").write_text('{"status":"ready"}')
    return path


def test_publication_boundary_and_duplicate_samples(tmp_path):
    path = dataset(tmp_path, [
        ("t95", "2026-07-19T06:00", 355),
        ("t95", "2026-07-20T06:00", 345),
        ("t95", "2026-07-21T06:00", 350),
        ("t95", "2026-07-21T06:00", 350),
        ("t95", "2026-07-21T06:00:01", 999),  # unpublished
        ("t95", "2026-07-22T06:00", 999),     # future
    ])
    result = quality_diagnostics(path, AT)
    assert result["can_authorize"] is False
    assert result["t95"]["measurement"]["age_hours"] == 4
    assert result["t95"]["measurement"]["available_at"] == AT
    estimate = result["t95"]["estimate"]
    assert estimate["value"] == 350
    assert len(estimate["inputs"]) == 3
    assert estimate["inputs"][-1]["age_hours"] == 52  # last-three policy, disclosed
    assert quality_diagnostics(path, "2026-07-21T09:59:59")["t95"]["measurement"]["value"] == 345


@pytest.mark.parametrize("flag,value", [("suspect", 354), ("invalid", 354), ("conflict", 354),
                                        ("gap", 354), ("flatline", 354), ("", None)])
def test_latest_bad_sample_is_not_replaced(tmp_path, flag, value):
    path = dataset(tmp_path, [("t95", "2026-07-20T06:00", 350),
                              ("t95", "2026-07-21T06:00", value, flag)])
    result = quality_diagnostics(path, AT)["t95"]
    assert result["measurement"]["timestamp"] == "2026-07-21T06:00:00"
    assert result["measurement"]["usable"] is False
    assert result["estimate"]["value"] is None


def test_disagreeing_duplicates_are_conflict(tmp_path):
    path = dataset(tmp_path, [("t95", "2026-07-21T06:00", 350), ("t95", "2026-07-21T06:00", 370)])
    target = quality_diagnostics(path, AT)["t95"]
    assert target["measurement"]["flags"] == ["conflict"]
    assert target["measurement"]["value"] is None
    assert target["estimate"]["value"] is None


def test_index_formula_publication_and_stale_cetane(tmp_path):
    path = dataset(tmp_path, [
        ("density", "2026-07-20T10:00", 840),
        ("t50", "2026-07-20T10:00", 280),
        ("cetane", "2026-06-21T10:00", 53),
        ("cetane", "2026-07-21T10:00", 50),
    ])
    result = quality_diagnostics(path, AT)
    # Reference computed with 40-digit Decimal arithmetic; kg/m3 -> g/ml.
    assert result["cetane"]["estimate"]["value"] == pytest.approx(53.18843143676569, abs=1e-7)
    lab = result["cetane"]["measurement"]
    assert lab["value"] == 53 and lab["age_hours"] == 720
    assert lab["freshness"] == "stale" and lab["usable"] is False
    assert result["can_authorize"] is False


@pytest.mark.parametrize("density,unit", [(0.84, "кг/м³"), (840, "g/ml"), (1100, "кг/м³")])
def test_index_rejects_wrong_units_and_range(tmp_path, density, unit):
    path = dataset(tmp_path, [("density", "2026-07-20T10:00", density, "", unit),
                              ("t50", "2026-07-20T10:00", 280)])
    estimate = quality_diagnostics(path, AT)["cetane"]["estimate"]
    assert estimate["value"] is None
    assert estimate["inputs"][0]["unit"] == unit  # never relabel a bad unit silently


def test_staleness_settings_do_not_extend_estimator_support(tmp_path):
    path = dataset(tmp_path, [("t95", "2026-07-19T10:00", 350),
                              ("density", "2026-07-19T09:59:59", 840),
                              ("t50", "2026-07-19T10:00", 280)])
    (path / "settings.json").write_text(json.dumps({"freshness_minutes": {"lims": 60*72}}))
    result = quality_diagnostics(path, AT)
    assert result["t95"]["estimate"]["value"] == 350  # inclusive boundary
    assert result["cetane"]["estimate"]["value"] is None
    assert result["cetane"]["estimate"]["inputs"][0]["freshness"] == "fresh"
    assert quality_diagnostics(path, "2026-07-21T10:00:01")["t95"]["estimate"]["value"] is None


def test_missing_data_and_api_validation(tmp_path, monkeypatch):
    dataset(tmp_path / "example", [("density", "2026-07-20T10:00", 840)])
    monkeypatch.setattr(app_module, "STORAGE", tmp_path)
    with TestClient(app_module.app) as client:
        response = client.get("/api/datasets/example/quality-diagnostics", params={"at": AT})
        assert response.status_code == 200
        result = response.json()
        assert result["t95"]["measurement"]["freshness"] == "missing"
        assert result["t95"]["estimate"]["value"] is None
        assert result["cetane"]["estimate"]["value"] is None
        for at in ["bad", "2026-01-01T00:00:00+03:00"]:
            assert client.get("/api/datasets/example/quality-diagnostics", params={"at": at}).status_code == 422
        assert client.get("/api/datasets/missing/quality-diagnostics", params={"at": AT}).status_code == 404


def test_estimates_do_not_hide_measured_violations(tmp_path):
    path = dataset(tmp_path, [("t95", "2026-07-19T06:00", 349),
                              ("t95", "2026-07-20T06:00", 350),
                              ("t95", "2026-07-21T06:00", 361),
                              ("cetane", "2026-07-01T06:00", 50),
                              ("density", "2026-07-20T10:00", 840),
                              ("t50", "2026-07-20T10:00", 280)])
    result = quality_diagnostics(path, AT)
    assert result["t95"]["estimate"]["value"] == 350
    assert result["t95"]["reference_warning"]
    assert result["cetane"]["estimate"]["value"] > 51
    assert result["cetane"]["reference_warning"]
    assert result["cetane"]["measurement"]["freshness"] == "stale"
    assert result["can_authorize"] is False
