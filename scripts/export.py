"""
export.py

Phase 4 - Export layer.

Reads gold.appliance_estimates (and silver.interval_features for temperature)
and writes two derived tables to the gold schema:

    gold.appliance_profile
        usage_date          - date
        appliance           - hvac | washer | dryer | cooking | baseline
        estimated_kwh       - attributed consumption for this appliance
        pct_of_daily_total  - share of that day's total (0–1)

    gold.daily_summary
        usage_date      - date
        total_kwh       - total daily consumption
        hvac_kwh        - HVAC attribution
        washer_kwh      - washer attribution
        dryer_kwh       - dryer attribution
        cooking_kwh     - cooking attribution
        baseline_kwh    - baseline (always-on) attribution
        avg_temp_c      - mean hourly temperature for the day

With --csv, also writes those tables to CSV files under --out-dir:
    reports/appliance_profile.csv
    reports/daily_summary.csv

Usage:
    python scripts/export.py [--db data/smart_meter.duckdb]
                              [--out-dir reports]
                              [--csv]
                              [--reset]

Options:
    --db       DuckDB file path (default: data/smart_meter.duckdb)
    --out-dir  Output directory for CSV files when --csv is set (default: reports)
    --csv      Also export gold tables to CSV files
    --reset    Drop and recreate gold export tables before loading
"""

import argparse
import duckdb
import pandas as pd
from pathlib import Path


# ---------------------------------------------------------------------------
# CSV file names
# ---------------------------------------------------------------------------

APPLIANCE_PROFILE_CSV = "appliance_profile.csv"
DAILY_SUMMARY_CSV     = "daily_summary.csv"


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

DDL_GOLD_SCHEMA = "CREATE SCHEMA IF NOT EXISTS gold"

DDL_APPLIANCE_PROFILE = """
CREATE TABLE IF NOT EXISTS gold.appliance_profile (
    usage_date          DATE,
    appliance           VARCHAR,
    estimated_kwh       DOUBLE,
    pct_of_daily_total  DOUBLE
)
"""

DDL_DAILY_SUMMARY = """
CREATE TABLE IF NOT EXISTS gold.daily_summary (
    usage_date      DATE,
    total_kwh       DOUBLE,
    hvac_kwh        DOUBLE,
    washer_kwh      DOUBLE,
    dryer_kwh       DOUBLE,
    cooking_kwh     DOUBLE,
    baseline_kwh    DOUBLE,
    avg_temp_c      DOUBLE
)
"""

DDL_DROP_APPLIANCE_PROFILE = "DROP TABLE IF EXISTS gold.appliance_profile"
DDL_DROP_DAILY_SUMMARY     = "DROP TABLE IF EXISTS gold.daily_summary"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_appliance_profile(con):
    """One row per (date, appliance) with estimated_kwh and share of daily total."""
    return con.execute("""
        SELECT
            g.usage_date,
            g.appliance,
            round(g.estimated_kwh, 4)                           AS estimated_kwh,
            round(g.estimated_kwh / daily.total_kwh, 6)         AS pct_of_daily_total
        FROM gold.appliance_estimates g
        JOIN (
            SELECT usage_date, sum(estimated_kwh) AS total_kwh
            FROM gold.appliance_estimates
            GROUP BY usage_date
        ) daily USING (usage_date)
        ORDER BY g.usage_date, g.appliance
    """).fetchdf()


def load_daily_summary(con):
    """One row per date with per-appliance kWh columns and avg outdoor temperature."""
    return con.execute("""
        SELECT
            g.usage_date,
            round(sum(g.estimated_kwh),                                                     4) AS total_kwh,
            round(sum(CASE WHEN g.appliance = 'hvac'     THEN g.estimated_kwh ELSE 0 END), 4) AS hvac_kwh,
            round(sum(CASE WHEN g.appliance = 'washer'   THEN g.estimated_kwh ELSE 0 END), 4) AS washer_kwh,
            round(sum(CASE WHEN g.appliance = 'dryer'    THEN g.estimated_kwh ELSE 0 END), 4) AS dryer_kwh,
            round(sum(CASE WHEN g.appliance = 'cooking'  THEN g.estimated_kwh ELSE 0 END), 4) AS cooking_kwh,
            round(sum(CASE WHEN g.appliance = 'baseline' THEN g.estimated_kwh ELSE 0 END), 4) AS baseline_kwh,
            round(avg(i.temp_c), 2)                                                            AS avg_temp_c
        FROM gold.appliance_estimates g
        JOIN (
            SELECT CAST(interval_start_dt AS DATE) AS usage_date,
                   avg(temp_c) AS temp_c
            FROM silver.interval_features
            GROUP BY 1
        ) i USING (usage_date)
        GROUP BY g.usage_date
        ORDER BY g.usage_date
    """).fetchdf()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_to_gold(con, profile_df, summary_df):
    """Insert DataFrames into gold export tables. Skips dates already loaded. Returns rows inserted."""
    loaded_dates = set(
        row[0] for row in con.execute(
            "SELECT DISTINCT usage_date FROM gold.appliance_profile"
        ).fetchall()
    )

    new_profile = profile_df[
        ~pd.to_datetime(profile_df["usage_date"]).dt.date.isin(loaded_dates)
    ]
    new_summary = summary_df[
        ~pd.to_datetime(summary_df["usage_date"]).dt.date.isin(loaded_dates)
    ]

    if len(new_profile) > 0:
        con.execute("INSERT INTO gold.appliance_profile SELECT * FROM new_profile")
    if len(new_summary) > 0:
        con.execute("INSERT INTO gold.daily_summary SELECT * FROM new_summary")

    return len(new_profile), len(new_summary)


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_csv(df, path):
    """Write DataFrame to CSV, overwriting any existing file."""
    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 4: write appliance profile and daily summary to gold DuckDB tables"
    )
    parser.add_argument("--db",      default="data/smart_meter.duckdb",
                        help="DuckDB file path (default: data/smart_meter.duckdb)")
    parser.add_argument("--out-dir", default="reports",
                        help="Output directory for CSV files when --csv is set (default: reports)")
    parser.add_argument("--csv",     action="store_true",
                        help="Also export gold tables to CSV files")
    parser.add_argument("--reset",   action="store_true",
                        help="Drop and recreate gold export tables before loading")
    args = parser.parse_args()

    db_path = Path(args.db)

    con = duckdb.connect(str(db_path))
    con.execute(DDL_GOLD_SCHEMA)

    if args.reset:
        con.execute(DDL_DROP_APPLIANCE_PROFILE)
        con.execute(DDL_DROP_DAILY_SUMMARY)
        print("Gold export tables dropped and will be recreated.")

    con.execute(DDL_APPLIANCE_PROFILE)
    con.execute(DDL_DAILY_SUMMARY)

    print("Loading gold.appliance_estimates...")
    profile = load_appliance_profile(con)
    print(f"  {len(profile):,} rows  ({profile['appliance'].nunique()} appliances"
          f"  ×  {profile['usage_date'].nunique()} days)")

    print("Loading daily summary with temperature...")
    summary = load_daily_summary(con)
    print(f"  {len(summary):,} daily rows  |  "
          f"temp range {summary['avg_temp_c'].min():.1f}–{summary['avg_temp_c'].max():.1f} °C")

    n_profile, n_summary = load_to_gold(con, profile, summary)

    print(f"\nDuckDB write complete.")
    print(f"  Inserted : {n_profile:,} rows → gold.appliance_profile")
    print(f"  Inserted : {n_summary:,} rows → gold.daily_summary")
    print(f"  DB       : {db_path}")

    if args.csv:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        profile_path = out_dir / APPLIANCE_PROFILE_CSV
        summary_path = out_dir / DAILY_SUMMARY_CSV

        print(f"\nExporting to CSV...")
        write_csv(profile, profile_path)
        print(f"  {profile_path}  ({len(profile):,} rows)")
        write_csv(summary, summary_path)
        print(f"  {summary_path}  ({len(summary):,} rows)")

    print(f"\nSample gold.daily_summary (first 5 rows):")
    print(summary.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
