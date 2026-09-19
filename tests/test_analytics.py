import json
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend import analytics as a
from backend.config import REGISTRY
from backend.formulas import evaluate_expression, formula_results
from backend.ingest import SCHEMA


def make_dataset(path, measurements):
    path.mkdir(exist_ok=True)
    rows = []
    metrics = {}
    for index, (mid, time, value, flags) in enumerate(measurements):
        source = "lims" if mid.startswith("lims.") else "pak" if mid.startswith("pak.") else "kip"
        metrics[mid] = {
            "id": mid,
            "label": mid,
            "source": source,
            "unit": "мг/кг" if source != "kip" else None,
            "plant": "ht",
            "code": mid,
            "group": "test",
        }
        rows.append(
            {
                "metric_id": mid,
                "timestamp": datetime.fromisoformat(time),
                "value": value,
                "source": source,
                "unit": metrics[mid]["unit"],
                "flags": flags,
                "source_file": "input.csv",
                "source_row": index + 2,
            }
        )
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path / "observations.parquet")
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "id": path.name,
                "status": "ready",
                "metrics": list(metrics.values()),
                "sources": [],
                "issues": [],
                "assumptions": [],
            }
        )
    )
    return path


def test_snapshot_no_future_and_lab_delta_is_previous_probe(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [
            ("avt.T1", "2025-01-01T12:00:00", 100, ""),
            ("avt.T1", "2025-01-01T12:10:00", 900, ""),
            ("lims.ht.2.Mg.Sulfur", "2024-12-31T10:00:00", 8, ""),
            ("lims.ht.2.Mg.Sulfur", "2025-01-01T10:00:00", 9, ""),
            ("lims.ht.2.Mg.Sulfur", "2025-01-02T10:00:00", 20, ""),
        ],
    )
    result = {v["metric_id"]: v for v in a.snapshot(path, "2025-01-01T12:03:00")["values"]}
    assert result["avt.T1"]["value"] == 100
    assert result["avt.T1"]["age_minutes"] == 3
    lab_result = {v["metric_id"]: v for v in a.snapshot(path, "2025-01-01T14:03:00")["values"]}
    assert lab_result["lims.ht.2.Mg.Sulfur"]["delta"] == 1
    assert lab_result["derived.sulfur_margin.lims"]["value"] == 1
    stale = {v["metric_id"]: v for v in a.snapshot(path, "2025-01-05T12:03:00")["values"]}
    assert stale["lims.ht.2.Mg.Sulfur"]["freshness"] == "stale"


def test_lims_sample_becomes_visible_four_hours_after_sampling(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [("lims.ht.2.Mg.Sulfur", "2025-01-01T10:00:00", 8, "")],
    )
    before = {v["metric_id"]: v for v in a.snapshot(path, "2025-01-01T13:59:00")["values"]}
    after = {v["metric_id"]: v for v in a.snapshot(path, "2025-01-01T14:00:00")["values"]}
    assert before["lims.ht.2.Mg.Sulfur"]["value"] is None
    assert after["lims.ht.2.Mg.Sulfur"]["value"] == 8
    assert after["lims.ht.2.Mg.Sulfur"]["available_at"] == "2025-01-01T14:00:00"


def test_expert_formula_overrides_stale_manifest_and_resolves_available_lims(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [
            ("ht.F9", "2025-01-01T12:00:00", 10, ""),
            ("ht.F2", "2025-01-01T12:00:00", 20, ""),
            ("ht.T6", "2025-01-01T12:00:00", 300, ""),
            ("lims.ht.2.95%.T", "2025-01-01T08:00:00", 350, ""),
        ],
    )
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["formulas"] = [
        {"id": "24-2000:GODT:T95", "expression": "1", "version": "old", "status": "invalid"}
    ]
    (path / "manifest.json").write_text(json.dumps(manifest))
    result = next(
        item
        for item in formula_results(path, "2025-01-01T12:00:00")["formulas"]
        if item["id"] == "24-2000:GODT:T95"
    )
    assert result["version"] == "expert-2026-09-13"
    assert result["result"] == pytest.approx(310.3035)


def test_expert_formula_corrections_are_canonical():
    formulas = {item["id"]: item for item in json.loads(REGISTRY.read_text())["formulas"]}
    expected = {
        "24-2000:GODT:T90": "162.998+0.12945*T12+59.57*(F15/2000)+0.00036*W7+0.26366*T23-424.72638*F1/F26",
        "24-2000:GODT:T50": "44.625+10.0224*P13+0.06981*F9+0.471*T6",
        "24-2000:GODT:CloudPoint": "0.0002*F22+0.0021*W7+0.00008*F25-0.30656*F1+0.12018*T6+0.01916*F9-48.254-0.05249*T16+0.00011",
        "24-2000:GODT:CFPP": "0.22088*T23-102.375-47.75834*P8+0.03862*F9+43.60207*W7+43.81849*P24",
        "24-2000:GODT:T95": "0.03814*F9-9.201-0.00002*F2+0.50*T6+0.48321*LIMS:24-2000.Pipeline.95%.T",
        "AVT6:240-350:CFPP": "31,40363 - 0,06784xT33 + 17,411xP67 - 8,11544xP4 - 0,47309x(F65/F32+F30)",
    }
    assert {key: formulas[key]["expression"] for key in expected} == expected
    assert all(formulas[key]["status"] == "experimental" for key in expected)


def test_period_raw_lab_count_boundary_and_pak_coverage(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [
            ("lims.ht.2.Mg.Sulfur", "2025-01-01T00:00:00", 10, ""),
            ("lims.ht.2.Mg.Sulfur", "2025-01-01T00:30:00", 12, ""),
            ("lims.ht.2.Mg.Sulfur", "2025-01-01T01:00:00", 90, ""),
            ("pak.ht.Mg.Sulfur", "2025-01-01T00:00:00", 10, ""),
            ("pak.ht.Mg.Sulfur", "2025-01-01T00:10:00", 12, ""),
            ("pak.ht.Mg.Sulfur", "2025-01-01T00:20:00", 12, "flatline"),
            ("pak.ht.Mg.Sulfur", "2025-01-01T00:50:00", 8, ""),
        ],
    )
    result = a.summary(path, "2025-01-01T00:00:00", "2025-01-01T01:00:00")
    sulfur = result["sulfur"]
    assert sulfur["lab_count"] == 2
    assert sulfur["lab_exceed_count"] == 1
    assert sulfur["lab_exceed_fraction"] == 0.5
    assert sulfur["pak_observed_minutes"] == 30
    assert sulfur["pak_exceed_minutes"] == 10
    assert sulfur["pak_suspect_minutes"] == 10
    assert sulfur["pak_coverage_fraction"] == 0.5
    lab = next(m for m in result["metrics"] if m["metric_id"] == "lims.ht.2.Mg.Sulfur")
    assert lab["median"] == 11 and lab["count"] == 2
    assert result["agreement"][0]["n"] == 1  # second lab too far from last trustworthy PAK


def test_statistics_exclusion_comparison_extremes_and_histogram(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [
            ("avt.T1", "2024-12-31T00:00:00", 4, ""),
            ("avt.T1", "2025-01-01T00:00:00", 1, ""),
            ("avt.T1", "2025-01-01T01:00:00", 3, ""),
            ("avt.T1", "2025-01-01T02:00:00", 100, "suspect"),
            ("avt.T1", "2025-01-01T03:00:00", 1000, "conflict"),
            ("avt.T1", "2025-01-01T04:00:00", None, "invalid"),
        ],
    )
    args = (path, "2025-01-01T00:00:00", "2025-01-02T00:00:00")
    row = a.summary(*args)["metrics"][0]
    assert row["count"] == 3 and row["median"] == 3 and row["max"] == 100
    assert row["p05"] == pytest.approx(1.2)
    assert row["p95"] == pytest.approx(90.3)
    assert row["iqr"] == pytest.approx(49.5)
    assert row["invalid_count"] == 2 and row["suspect_count"] == 1
    assert row["previous_median"] == 4 and row["median_change"] == -1
    assert row["max_at"] == "2025-01-01T02:00:00"
    row = a.summary(*args, True)["metrics"][0]
    assert row["count"] == 2 and row["median"] == 2 and row["mean"] == 2
    hist = a.distribution(path, "avt.T1", args[1], args[2], True)
    assert sum(b["count"] for b in hist["bins"]) == 2
    assert hist["quartiles"] == [1, 1.5, 2, 2.5, 3]


def test_aggregation_preserves_peak_and_derivation_is_per_frame(tmp_path):
    measurements = []
    for i in range(144):
        time = (datetime(2025, 1, 1) + timedelta(minutes=i * 10)).isoformat()
        measurements.extend(
            [("avt.T33", time, 999 if i == 80 else 330, ""), ("avt.T20", time, 120, "")]
        )
    path = make_dataset(tmp_path / "data", measurements)
    result = a.series(
        path, ["derived.avt.delta_k2"], "2025-01-01T00:00:00", "2025-01-02T00:00:00", 50
    )
    points = result["series"][0]["points"]
    assert len(points) <= 50 and max(p["max"] for p in points) == 879
    assert result["series"][0]["aggregated"]


def test_formula_restricted_arithmetic_and_division_guard():
    assert evaluate_expression("791.2 + 0.5*T1 - F1/F2", {"T1": 100, "F1": 10, "F2": 2}) == 836.2
    with pytest.raises(ValueError, match="Деление"):
        evaluate_expression("F1/F2", {"F1": 1, "F2": 0})
    with pytest.raises(ValueError):
        evaluate_expression('__import__("os").system("echo bad")', {})
    with pytest.raises(ValueError):
        evaluate_expression("T1 ** 100000", {"T1": 5})


def test_invalid_or_timezone_dates_rejected():
    with pytest.raises(ValueError):
        a.interval("2025-01-01", "2024-01-01")
    with pytest.raises(ValueError):
        a.parse_time("2025-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        a.parse_time("bad")


def test_distillation_never_combines_different_probe_times(tmp_path):
    path = make_dataset(
        tmp_path / "data",
        [
            ("lims.ht.2.IBP.T", "2025-01-01T10:00:00", 190, ""),
            ("lims.ht.2.50%.T", "2025-01-01T10:00:00", 270, ""),
            ("lims.ht.2.95%.T", "2025-01-02T10:00:00", 345, ""),
        ],
    )
    result = a.distillation(path, "2025-01-02T11:00:00")
    assert result["timestamp"] == "2025-01-01T10:00:00"
    assert [p["fraction"] for p in result["points"]] == [0, 50]
    assert result["age_minutes"] == 1500
