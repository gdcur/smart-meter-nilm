"""
ingest.py

Phase 1 - Bronze layer ingestion.

Walks the data/sample/YYYY/MM/ folder tree, parses each ERCOT interval CSV,
validates the data, and loads it into DuckDB as the bronze layer.

Bronze table: raw.meter_intervals
    esiid               VARCHAR   - meter identifier (apostrophe stripped)
    usage_date          DATE      - date of usage
    revision_date       TIMESTAMP - when ERCOT last revised this record
    interval_start      VARCHAR   - start of 15-min interval
    interval_end        VARCHAR   - end of 15-min interval
    interval_start_dt   TIMESTAMP - full timestamp (usage_date + interval_start)
    usage_kwh           DOUBLE    - energy consumed in kWh
    estimated_actual    VARCHAR   - 'A' actual or 'E' estimated
    flow_direction      VARCHAR   - 'Consumption' or 'SurplusGeneration'
    source_file         VARCHAR   - relative path of source CSV (lineage)
    loaded_at           TIMESTAMP - when this record was loaded

Usage:
    python scripts/ingest.py [--data-dir data/sample] [--db data/smart_meter.duckdb] [--reset]

Options:
    --data-dir  Root folder to scan for YYYY/MM/YYYYMMDD.csv files (default: data/sample)
    --db        DuckDB file path (default: data/smart_meter.duckdb)
    --reset     Drop and recreate the bronze table before loading
"""

import argparse
import duckdb
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL_RAW_SCHEMA = "CREATE SCHEMA IF NOT EXISTS raw"

DDL_BRONZE_TABLE = """
CREATE TABLE IF NOT EXISTS raw.meter_intervals (
    esiid               VARCHAR,
    usage_date          DATE,
    revision_date       TIMESTAMP,
    interval_start      VARCHAR,
    interval_end        VARCHAR,
    interval_start_dt   TIMESTAMP,
    usage_kwh           DOUBLE,
    estimated_actual    VARCHAR,
    flow_direction      VARCHAR,
    source_file         VARCHAR,
    loaded_at           TIMESTAMP
)
"""

DDL_DROP_TABLE = "DROP TABLE IF EXISTS raw.meter_intervals"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_file(path, data_root):
    """
    Reads one ERCOT interval CSV and returns a clean DataFrame.
    Handles:
      - Leading apostrophe on ESIID
      - Leading whitespace in USAGE_START_TIME / USAGE_END_TIME
      - Mixed date formats (MM/DD/YYYY and MM/DD/YYYY HH:MM:SS)
    """
    df = pd.read_csv(path, dtype=str)

    # Normalize column names
    df.columns = [c.strip().upper() for c in df.columns]

    # Strip leading apostrophe from ESIID
    df["ESIID"] = df["ESIID"].str.lstrip("'").str.strip()

    # Strip whitespace from time columns
    df["USAGE_START_TIME"] = df["USAGE_START_TIME"].str.strip()
    df["USAGE_END_TIME"]   = df["USAGE_END_TIME"].str.strip()

    # Parse dates
    df["USAGE_DATE"]    = pd.to_datetime(df["USAGE_DATE"], format="%m/%d/%Y").dt.date
    df["REVISION_DATE"] = pd.to_datetime(df["REVISION_DATE"], format="%m/%d/%Y %H:%M:%S")

    # Build full interval start timestamp
    df["interval_start_dt"] = pd.to_datetime(
        df["USAGE_DATE"].astype(str) + " " + df["USAGE_START_TIME"]
    )

    # Cast kWh
    df["USAGE_KWH"] = pd.to_numeric(df["USAGE_KWH"], errors="coerce")

    # Lineage
    df["source_file"] = str(path.relative_to(data_root))
    df["loaded_at"]   = datetime.now(timezone.utc).replace(tzinfo=None)

    return df.rename(columns={
        "ESIID":                         "esiid",
        "USAGE_DATE":                    "usage_date",
        "REVISION_DATE":                 "revision_date",
        "USAGE_START_TIME":              "interval_start",
        "USAGE_END_TIME":                "interval_end",
        "USAGE_KWH":                     "usage_kwh",
        "ESTIMATED_ACTUAL":              "estimated_actual",
        "CONSUMPTION_SURPLUSGENERATION": "flow_direction",
    })[[
        "esiid", "usage_date", "revision_date",
        "interval_start", "interval_end", "interval_start_dt",
        "usage_kwh", "estimated_actual", "flow_direction",
        "source_file", "loaded_at",
    ]]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(df, source_file):
    """
    Runs basic validation checks. Logs warnings but does not abort.
    Returns the cleaned DataFrame (invalid rows flagged, not dropped).
    """
    issues = []

    # Expected 96 intervals per day
    if len(df) != 96:
        issues.append(f"expected 96 intervals, got {len(df)}")

    # No null kWh
    null_kwh = df["usage_kwh"].isna().sum()
    if null_kwh > 0:
        issues.append(f"{null_kwh} null usage_kwh values")

    # No negative kWh
    neg_kwh = (df["usage_kwh"] < 0).sum()
    if neg_kwh > 0:
        issues.append(f"{neg_kwh} negative usage_kwh values")

    # No duplicate intervals
    dupes = df.duplicated(subset=["esiid", "interval_start_dt"]).sum()
    if dupes > 0:
        issues.append(f"{dupes} duplicate intervals")

    if issues:
        print(f"  WARN [{source_file}]: {'; '.join(issues)}")

    return df


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_to_duckdb(con, df):
    """Inserts DataFrame into raw.meter_intervals. Returns rows inserted."""
    con.execute("INSERT INTO raw.meter_intervals SELECT * FROM df")
    return len(df)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ingest ERCOT interval CSVs into DuckDB bronze layer")
    parser.add_argument("--data-dir", default="data/sample",           help="Root folder to scan")
    parser.add_argument("--db",       default="data/smart_meter.duckdb", help="DuckDB file path")
    parser.add_argument("--reset",    action="store_true",              help="Drop and recreate bronze table")
    args = parser.parse_args()

    data_root = Path(args.data_dir)
    db_path   = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Find all CSV files under YYYY/MM/YYYYMMDD.csv
    files = sorted(data_root.glob("????/??/????????.csv"))
    if not files:
        print(f"No CSV files found under {data_root}")
        return

    print(f"Found {len(files)} files in {data_root}")

    con = duckdb.connect(str(db_path))
    con.execute(DDL_RAW_SCHEMA)

    if args.reset:
        con.execute(DDL_DROP_TABLE)
        print("Bronze table dropped and will be recreated.")

    con.execute(DDL_BRONZE_TABLE)

    # Skip already-loaded files
    loaded_files = set(
        row[0] for row in con.execute(
            "SELECT DISTINCT source_file FROM raw.meter_intervals"
        ).fetchall()
    )

    total_rows = 0
    skipped    = 0
    errors     = 0

    for path in files:
        rel = str(path.relative_to(data_root))
        if rel in loaded_files:
            skipped += 1
            continue
        try:
            df = parse_file(path, data_root)
            df = validate(df, rel)
            rows = load_to_duckdb(con, df)
            total_rows += rows
        except Exception as e:
            print(f"  ERROR [{rel}]: {e}")
            errors += 1

    con.close()

    print(f"\nIngestion complete.")
    print(f"  Loaded : {total_rows} rows from {len(files) - skipped - errors} files")
    print(f"  Skipped: {skipped} already-loaded files")
    print(f"  Errors : {errors}")
    print(f"  DB     : {db_path}")


if __name__ == "__main__":
    main()
