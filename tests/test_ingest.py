import json

import openpyxl
import pandas as pd
import pyarrow.parquet as pq

from backend.ingest import import_dataset


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_imports_partial_kip_with_quality_flags(tmp_path):
    source = tmp_path / "avt_tags.csv"
    times = pd.date_range("2025-01-01", periods=39, freq="10min")
    frame = pd.DataFrame(
        {
            "date": list(times) + [times[-1], times[-1]],
            "T1": [1.0] * 39 + [1.0, 2.0],
            "F26": list(range(39)) + [38, "bad"],
        }
    )
    frame.to_csv(source, index=False)
    out = tmp_path / "dataset"
    manifest = import_dataset([source], out, "Tiny")
    table = pq.read_table(out / "observations.parquet").to_pandas()

    assert manifest["status"] == "ready"
    assert {m["id"] for m in manifest["metrics"]} == {"avt.T1", "avt.F26"}
    by_id = {m["id"]: m for m in manifest["metrics"]}
    assert by_id["avt.T1"]["unit"] == "°C" and by_id["avt.T1"]["unit_status"] == "inferred"
    assert by_id["avt.F26"]["unit"] is None
    assert set(table[table.metric_id == "avt.T1"].unit) == {"°C"}
    assert manifest["telemetry_start"] == "2025-01-01T00:00:00"
    assert manifest["sources"][0]["rows"] == len(table) == 80
    assert manifest["sources"][0]["duplicate_count"] == 2
    t1 = table[table.metric_id == "avt.T1"]
    assert "flatline" not in t1.iloc[35]["flags"]
    assert "flatline" in t1.iloc[36]["flags"]  # flag starts at six elapsed hours
    last = table[(table.metric_id == "avt.T1") & (table.timestamp == times[-1])]
    assert len(last) == 2 and all("conflict" in flags for flags in last["flags"])
    bad = table[(table.metric_id == "avt.F26") & table.value.isna()]
    assert len(bad) == 1 and bad.iloc[0]["flags"] == "conflict|invalid"
    assert json.loads((out / "manifest.json").read_text())["id"] == "dataset"


def test_lims_pairs_keep_independent_timestamps_units_and_ids(tmp_path):
    source = tmp_path / "ЛИМСы.xlsx"
    _xlsx(
        source,
        [
            [
                "Установка 'АВТ'. Точка отбора '2.1'. Продукт 'ДТ'",
                None,
                "Установка 'Гидроочистка'. Точка отбора '2'. Продукт 'ДТ'",
                None,
            ],
            ["D15", None, "Mg.Sulfur", None],
            ["кг/м3", None, "мг/кг", None],
            ["Количество значений: 1", None, "Количество значений: 1", None],
            ["2025-01-01 01:00", 840, "2025-01-03 02:00", 8.2],
        ],
    )
    out = tmp_path / "dataset"
    manifest = import_dataset([source], out, "Labs")
    data = pq.read_table(out / "observations.parquet").to_pandas().set_index("metric_id")

    assert set(data.index) == {"lims.avt.2.1.D15", "lims.ht.2.Mg.Sulfur"}
    assert data.loc["lims.avt.2.1.D15", "timestamp"] == pd.Timestamp("2025-01-01 01:00")
    assert data.loc["lims.ht.2.Mg.Sulfur", "timestamp"] == pd.Timestamp("2025-01-03 02:00")
    assert data.loc["lims.ht.2.Mg.Sulfur", "unit"] == "мг/кг"
    sulfur = next(m for m in manifest["metrics"] if m["id"] == "lims.ht.2.Mg.Sulfur")
    assert sulfur["primary"] is True
    assert not data["flags"].str.contains("flatline").any()


def test_flatline_resets_after_gap_and_never_marks_lims(tmp_path):
    kip = tmp_path / "avt_tags.csv"
    times = list(pd.date_range("2025-01-01", periods=37, freq="10min"))
    times += list(pd.date_range("2025-01-02", periods=37, freq="10min"))
    pd.DataFrame({"date": times, "T1": [5.0] * len(times)}).to_csv(kip, index=False)
    lims = tmp_path / "ЛИМСы.xlsx"
    lab_rows = [
        ["Установка 'АВТ'. Точка отбора '1'. Продукт 'ДТ'", None],
        ["CFPP", None],
        ["°С", None],
        ["Количество значений: 2", None],
        ["2025-01-01", -5],
        ["2025-01-02", -5],
    ]
    _xlsx(lims, lab_rows)
    out = tmp_path / "dataset"
    import_dataset([kip, lims], out, "Gap")
    data = pq.read_table(out / "observations.parquet").to_pandas()
    kip_data = data[data.metric_id == "avt.T1"].reset_index(drop=True)
    assert kip_data.iloc[36]["flags"] == "flatline"
    assert kip_data.iloc[37]["flags"] == ""
    assert not data[data.source == "lims"]["flags"].str.contains("flatline").any()


def test_pak_pairs_and_empty_bad_sources_are_graceful(tmp_path):
    pak = tmp_path / "Выгрузка ПАК.xlsx"
    _xlsx(
        pak,
        [
            ["24-2000:Mg.Sulfur", None, None, "24-2000:D15", None],
            ["ppm", None, None, "кг/м3", None],
            ["2025-01-01", 7.1, None, "2025-02-01", 831.2],
            ["2025-01-02", "bad", None, None, None],
        ],
    )
    bad = tmp_path / "ЛИМС_bad.xlsx"
    bad.write_bytes(b"not an xlsx")
    out = tmp_path / "dataset"
    manifest = import_dataset([pak, bad], out, "PAK")
    data = pq.read_table(out / "observations.parquet").to_pandas()

    assert set(data.metric_id) == {"pak.ht.Mg.Sulfur", "pak.ht.D15"}
    assert len(data) == 3
    assert data[data.value.isna()].iloc[0]["flags"] == "invalid"
    assert any(i["code"] == "source_error" for i in manifest["issues"])
    assert manifest["start"] == "2025-01-01T00:00:00"
    assert manifest["end"] == "2025-02-01T00:00:00"


def test_no_recognized_files_still_writes_empty_parquet(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("nothing")
    out = tmp_path / "dataset"
    manifest = import_dataset([source], out, "Empty")
    assert manifest["status"] == "error"
    assert manifest["sources"] == [] and manifest["metrics"] == []
    assert pq.read_table(out / "observations.parquet").num_rows == 0
