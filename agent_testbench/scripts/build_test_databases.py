from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[4]
BENCH_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = BENCH_ROOT / "databases"
CSV_DIR = DB_DIR / "csv"
JSON_DIR = DB_DIR / "json"


SOURCE_FILES = {
    "avt": ROOT / "data" / "avt_tags.csv",
    "unit_242000": ROOT / "data" / "242000_tags.csv",
    "lims": ROOT / "ЛИМСы 01.01.2023 - н.в_ (2).xlsx",
    "pak": ROOT / "Выгрузка ПАК 01.01.2023 - н.в_.xlsx",
    "tag_catalog": ROOT / "Теги_хакатон.xlsx",
}


CONTROL_CANDIDATES = {
    "T5",
    "T6",
    "F9",
    "F14",
    "F15",
    "F26",
    "F30",
    "F31",
    "F32",
    "F34",
    "F45",
    "F59",
}

CRITICAL_TAGS = ["T5", "P13", "F26", "F30", "F9"]


def normalize_lims_unit(analyte: str, source_unit: object) -> str:
    if analyte == "D15":
        return "kg/m3"
    if "Sulfur" in analyte:
        return "mg/kg"
    if analyte in {
        "50%.T",
        "90%.T",
        "95%.T",
        "CloudPoint",
        "CFPP",
        "PourPoint",
        "FlashPoint",
    }:
        return "C"
    return "" if pd.isna(source_unit) else str(source_unit)


def ensure_dirs() -> None:
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


def read_telemetry(path: Path, rows: int) -> pd.DataFrame:
    df = pd.read_csv(path, nrows=rows)
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_avt() -> pd.DataFrame:
    df = read_telemetry(SOURCE_FILES["avt"], rows=96)
    df["test_case"] = "base"
    if "F30" in df.columns:
        df.loc[20, "F30"] = pd.NA
        df.loc[20, "test_case"] = "missing_avt_flow"
    if "T66" in df.columns:
        df.loc[40, "T66"] = df["T66"].median() + 18
        df.loc[40, "test_case"] = "temperature_jump"
    return df


def build_unit_242000() -> pd.DataFrame:
    df = read_telemetry(SOURCE_FILES["unit_242000"], rows=96)
    df["test_case"] = "base"
    if "T5" in df.columns:
        df.loc[24, "T5"] = df["T5"].median() + 15
        df.loc[24, "test_case"] = "high_reactor_temperature"
    if "F26" in df.columns:
        df.loc[36, "F26"] = pd.NA
        df.loc[36, "test_case"] = "missing_feed_flow"
    if "F9" in df.columns:
        flat_value = df.loc[50, "F9"]
        df.loc[49:56, "F9"] = flat_value
        df.loc[49:56, "test_case"] = "flatline_signal"
    return df


def parse_lims() -> pd.DataFrame:
    raw = pd.read_excel(SOURCE_FILES["lims"], sheet_name="Лист1", header=None)
    groups = raw.iloc[0].ffill()
    rows = []

    for date_col in range(0, raw.shape[1] - 1, 2):
        value_col = date_col + 1
        group = groups.iloc[date_col]
        analyte = raw.iat[1, date_col]
        unit = raw.iat[2, date_col]
        if pd.isna(group) or pd.isna(analyte):
            continue

        pair = raw.iloc[4:, [date_col, value_col]].copy()
        pair.columns = ["timestamp", "value"]
        pair["timestamp"] = pd.to_datetime(pair["timestamp"], errors="coerce")
        pair["value"] = pd.to_numeric(pair["value"], errors="coerce")
        pair = pair.dropna(subset=["timestamp", "value"])
        pair["source"] = "LIMS"
        pair["sampling_point"] = str(group)
        pair["analyte"] = str(analyte)
        pair["unit"] = normalize_lims_unit(str(analyte), unit)
        rows.append(pair)

    lims = pd.concat(rows, ignore_index=True)
    useful = {
        "Mg.Sulfur",
        "Mass.Sulfur",
        "D15",
        "50%.T",
        "90%.T",
        "95%.T",
        "CloudPoint",
        "CFPP",
        "PourPoint",
        "FlashPoint",
    }
    lims = lims[lims["analyte"].isin(useful)]
    lims = lims.sort_values("timestamp").head(240)
    return lims[
        ["timestamp", "source", "sampling_point", "analyte", "value", "unit"]
    ].reset_index(drop=True)


def parse_pak() -> pd.DataFrame:
    raw = pd.read_excel(SOURCE_FILES["pak"], sheet_name="Лист1", header=None)
    rows = []
    for date_col in [0, 3]:
        value_col = date_col + 1
        tag = raw.iat[0, date_col]
        unit = raw.iat[1, date_col]
        pair = raw.iloc[2:, [date_col, value_col]].copy()
        pair.columns = ["timestamp", "value"]
        pair["timestamp"] = pd.to_datetime(pair["timestamp"], errors="coerce")
        pair["value"] = pd.to_numeric(pair["value"], errors="coerce")
        pair = pair.dropna(subset=["timestamp", "value"])
        pair["source"] = "PAK"
        pair["tag"] = str(tag)
        pair["unit"] = "" if pd.isna(unit) else str(unit)
        rows.append(pair)
    pak = pd.concat(rows, ignore_index=True).sort_values("timestamp").head(240)
    return pak[["timestamp", "source", "tag", "value", "unit"]].reset_index(drop=True)


def build_tag_catalog() -> pd.DataFrame:
    kip = pd.read_excel(SOURCE_FILES["tag_catalog"], sheet_name="КИП")
    rows = []
    for _, row in kip.iterrows():
        for area, desc_col, tag_col in [
            ("AVT", "АВТ (описание)", "АВТ"),
            ("24-2000", "24-2000 (описание)", "24-2000"),
        ]:
            tag = row.get(tag_col)
            desc = row.get(desc_col)
            if pd.isna(tag) or pd.isna(desc):
                continue
            tag = str(tag).strip()
            rows.append(
                {
                    "area": area,
                    "tag": tag,
                    "description": str(desc).strip(),
                    "is_control_candidate": tag in CONTROL_CANDIDATES,
                    "is_critical_for_tests": tag in CRITICAL_TAGS,
                }
            )
    catalog = pd.DataFrame(rows).drop_duplicates(["area", "tag"])
    return catalog[catalog["is_control_candidate"] | catalog["is_critical_for_tests"]].head(40)


def build_vak_formulas() -> pd.DataFrame:
    raw = pd.read_excel(SOURCE_FILES["tag_catalog"], sheet_name="ВАК", header=None)
    rows = []
    for col in range(0, raw.shape[1] - 1, 2):
        unit_name = raw.iat[0, col]
        for row_idx in range(1, raw.shape[0]):
            indicator = raw.iat[row_idx, col]
            formula = raw.iat[row_idx, col + 1]
            if pd.isna(indicator) or pd.isna(formula):
                continue
            rows.append(
                {
                    "unit": str(unit_name),
                    "indicator": str(indicator),
                    "formula": str(formula),
                }
            )
    return pd.DataFrame(rows).head(24)


def latest_before(df: pd.DataFrame, timestamp: pd.Timestamp, group_col: str) -> dict:
    selected = {}
    for key, group in df.groupby(group_col):
        before = group[group["timestamp"] <= timestamp]
        if before.empty:
            continue
        row = before.sort_values("timestamp").iloc[-1]
        age_hours = (timestamp - row["timestamp"]).total_seconds() / 3600
        selected[str(key)] = {
            "value": None if pd.isna(row["value"]) else float(row["value"]),
            "unit": row.get("unit", ""),
            "timestamp": row["timestamp"].isoformat(),
            "age_hours": round(age_hours, 3),
        }
    return selected


def telemetry_snapshot(df: pd.DataFrame, timestamp: pd.Timestamp, columns: list[str]) -> dict:
    before = df[df["date"] <= timestamp].sort_values("date")
    if before.empty:
        return {}
    row = before.iloc[-1]
    result = {}
    for col in columns:
        if col in row.index:
            value = row[col]
            result[col] = None if pd.isna(value) else float(value)
    return result


def build_process_snapshots(
    avt: pd.DataFrame,
    unit_242000: pd.DataFrame,
    lims: pd.DataFrame,
    pak: pd.DataFrame,
) -> list[dict]:
    timestamps = [
        pd.Timestamp("2023-01-01 00:00:00"),
        pd.Timestamp("2023-01-01 04:00:00"),
        pd.Timestamp("2023-01-01 06:00:00"),
        pd.Timestamp("2023-01-01 09:20:00"),
    ]
    snapshots = []
    for idx, ts in enumerate(timestamps, start=1):
        latest_lims = latest_before(lims, ts, "analyte")
        latest_pak = latest_before(pak, ts, "tag")
        snapshots.append(
            {
                "snapshot_id": f"PS-{idx:03d}",
                "timestamp": ts.isoformat(),
                "avt_telemetry": telemetry_snapshot(
                    avt, ts, ["T6", "T33", "T42", "T48", "T66", "F30", "F31", "F32"]
                ),
                "unit_242000_telemetry": telemetry_snapshot(
                    unit_242000, ts, ["T5", "T6", "T12", "P13", "F9", "F15", "F26"]
                ),
                "latest_lims": latest_lims,
                "latest_pak": latest_pak,
                "features": {
                    "lims_available_count": len(latest_lims),
                    "pak_available_count": len(latest_pak),
                    "source_priority": "LIMS -> PAK -> VAK -> KIP",
                },
                "agent_results": {
                    "data_quality": None,
                    "quality_state": None,
                    "reliability_state": None,
                    "candidate_scenarios": None,
                    "safety_gate": None,
                    "ranking": None,
                },
            }
        )
    return snapshots


def build_agent_test_cases() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "case_id": "DQ-001",
                "target_agent": "dq_agent",
                "input_table": "unit_242000_telemetry",
                "condition": "F26 is missing for one timestamp",
                "expected_behavior": "return limited data_status and include F26 in missing_critical_tags",
            },
            {
                "case_id": "DQ-002",
                "target_agent": "dq_agent",
                "input_table": "unit_242000_telemetry",
                "condition": "F9 is unchanged for several sequential timestamps",
                "expected_behavior": "flag flatline_signal anomaly without stopping the pipeline",
            },
            {
                "case_id": "QA-001",
                "target_agent": "quality_agent",
                "input_table": "lims_measurements and pak_measurements",
                "condition": "LIMS and PAK are joined only by latest timestamp before ProcessState.timestamp",
                "expected_behavior": "choose trusted quality source by LIMS -> PAK -> VAK -> KIP priority",
            },
            {
                "case_id": "RA-001",
                "target_agent": "reliability_agent",
                "input_table": "unit_242000_telemetry",
                "condition": "T5 is above the local test baseline",
                "expected_behavior": "increase risk_score and forbid further T5 increase",
            },
            {
                "case_id": "SA-001",
                "target_agent": "scenario_agent",
                "input_table": "process_state_snapshots",
                "condition": "ProcessState has Quality and Reliability restrictions",
                "expected_behavior": "generate candidate_scenarios, not final operator recommendation",
            },
            {
                "case_id": "SG-001",
                "target_agent": "safety_gate",
                "input_table": "candidate_scenarios",
                "condition": "scenario forecast_sulfur_mg_kg exceeds 10 or changes forbidden parameter",
                "expected_behavior": "reject scenario and return rejected_scenarios with reason",
            },
        ]
    )


def write_sqlite(tables: dict[str, pd.DataFrame], snapshots: list[dict]) -> None:
    db_path = DB_DIR / "oilcode_agent_test.db"
    if db_path.exists():
        db_path.unlink()

    with sqlite3.connect(db_path) as conn:
        for name, df in tables.items():
            df.to_sql(name, conn, index=False)
        pd.DataFrame(
            [
                {
                    "snapshot_id": snap["snapshot_id"],
                    "timestamp": snap["timestamp"],
                    "process_state_json": json.dumps(snap, ensure_ascii=False),
                }
                for snap in snapshots
            ]
        ).to_sql("process_state_snapshots", conn, index=False)


def main() -> None:
    ensure_dirs()

    avt = build_avt()
    unit_242000 = build_unit_242000()
    lims = parse_lims()
    pak = parse_pak()
    tag_catalog = build_tag_catalog()
    vak_formulas = build_vak_formulas()
    test_cases = build_agent_test_cases()
    snapshots = build_process_snapshots(avt, unit_242000, lims, pak)

    tables = {
        "avt_telemetry": avt,
        "unit_242000_telemetry": unit_242000,
        "lims_measurements": lims,
        "pak_measurements": pak,
        "tag_catalog": tag_catalog,
        "vak_formulas": vak_formulas,
        "agent_test_cases": test_cases,
    }

    for name, df in tables.items():
        df.to_csv(CSV_DIR / f"{name}.csv", index=False)

    with (JSON_DIR / "process_state_snapshots.jsonl").open("w", encoding="utf-8") as fh:
        for snap in snapshots:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")

    write_sqlite(tables, snapshots)

    print(f"Created test databases in {DB_DIR}")
    print(f"CSV tables: {', '.join(tables)}")
    print(f"ProcessState snapshots: {len(snapshots)}")


if __name__ == "__main__":
    main()
