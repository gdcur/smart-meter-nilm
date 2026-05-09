"""
export.py

Phase 4 - Export layer.

Reads gold.appliance_estimates (and silver.interval_features for temperature)
from DuckDB and writes two CSVs to reports/:

    reports/appliance_profile.csv
        usage_date          - date
        appliance           - hvac | washer | dryer | cooking | baseline
        estimated_kwh       - attributed consumption for this appliance
        pct_of_daily_total  - share of that day's total (0–1)

    reports/daily_summary.csv
        usage_date      - date
        total_kwh       - total daily consumption
        hvac_kwh        - HVAC attribution
        washer_kwh      - washer attribution
        dryer_kwh       - dryer attribution
        cooking_kwh     - cooking attribution
        baseline_kwh    - baseline (always-on) attribution
        avg_temp_c      - mean hourly temperature for the day

Usage:
    python scripts/export.py [--db data/smart_meter.duckdb]
                              [--out-dir reports]
                              [--reset]

Options:
    --db       DuckDB file path (default: data/smart_meter.duckdb)
    --out-dir  Output directory for CSV files (default: reports)
    --reset    Delete existing report files before writing
"""

import argparse
import duckdb
import pandas as pd
from pathlib import Path


# ---------------------------------------------------------------------------
# Output file names
# ---------------------------------------------------------------------------

APPLIANCE_PROFILE = "appliance_profile.csv"
DAILY_SUMMARY     = "daily_summary.csv"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_appliance_profile(con):
    """
    Build the appliance profile: one row per (date, appliance) with
    estimated_kwh and its share of that day's total.
    """
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
    """
    Build the daily summary: one row per date with per-appliance kWh columns
    and the day's average outdoor temperature.
    """
    return con.execute("""
        SELECT
            g.usage_date,
            round(sum(g.estimated_kwh),                                    4) AS total_kwh,
            round(sum(CASE WHEN g.appliance = 'hvac'     THEN g.estimated_kwh ELSE 0 END), 4) AS hvac_kwh,
            round(sum(CASE WHEN g.appliance = 'washer'   THEN g.estimated_kwh ELSE 0 END), 4) AS washer_kwh,
            round(sum(CASE WHEN g.appliance = 'dryer'    THEN g.estimated_kwh ELSE 0 END), 4) AS dryer_kwh,
            round(sum(CASE WHEN g.appliance = 'cooking'  THEN g.estimated_kwh ELSE 0 END), 4) AS cooking_kwh,
            round(sum(CASE WHEN g.appliance = 'baseline' THEN g.estimated_kwh ELSE 0 END), 4) AS baseline_kwh,
            round(avg(i.temp_c), 2)                                        AS avg_temp_c
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
# Writer
# ---------------------------------------------------------------------------

def write_csv(df, path, reset):
    """Write DataFrame to CSV. With --reset, deletes the file first if it exists."""
    if reset and path.exists():
        path.unlink()
        print(f"  Deleted existing {path.name}")
    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 4: export appliance estimates to CSV reports"
    )
    parser.add_argument("--db",      default="data/smart_meter.duckdb",
                        help="DuckDB file path (default: data/smart_meter.duckdb)")
    parser.add_argument("--out-dir", default="reports",
                        help="Output directory for CSV files (default: reports)")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete existing report files before writing")
    args = parser.parse_args()

    db_path  = Path(args.db)
    out_dir  = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path), read_only=True)

    print("Loading gold.appliance_estimates...")
    profile = load_appliance_profile(con)
    print(f"  {len(profile):,} rows  ({profile['appliance'].nunique()} appliances"
          f"  ×  {profile['usage_date'].nunique()} days)")

    print("Loading daily summary with temperature...")
    summary = load_daily_summary(con)
    print(f"  {len(summary):,} daily rows  |  "
          f"temp range {summary['avg_temp_c'].min():.1f}–{summary['avg_temp_c'].max():.1f} °C")

    con.close()

    profile_path = out_dir / APPLIANCE_PROFILE
    summary_path = out_dir / DAILY_SUMMARY

    print(f"\nWriting reports...")
    write_csv(profile, profile_path, args.reset)
    print(f"  {profile_path}  ({len(profile):,} rows)")
    write_csv(summary, summary_path, args.reset)
    print(f"  {summary_path}  ({len(summary):,} rows)")

    print(f"\nExport complete.")
    print(f"\nSample daily_summary.csv (first 5 rows):")
    print(summary.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
