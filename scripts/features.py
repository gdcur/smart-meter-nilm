"""
features.py

Phase 2 - Silver layer feature engineering.

Reads from DuckDB bronze (raw.meter_intervals), enriches each 15-min interval
with time and weather features, aggregates to daily load summaries, and stores
both in the DuckDB silver schema.

Silver tables:
    silver.interval_features
        esiid               VARCHAR   - meter identifier
        interval_start_dt   TIMESTAMP - start of 15-min interval
        usage_kwh           DOUBLE    - energy consumed in kWh
        hour_of_day         INTEGER   - 0-23
        day_of_week         INTEGER   - 0=Mon … 6=Sun
        is_weekend          BOOLEAN
        is_peak             BOOLEAN   - True for hours 6-20 (6am–9pm)
        temp_c              DOUBLE    - hourly dry bulb temperature (°C)
        ac_proxy            BOOLEAN   - True when temp_c > 23

    silver.daily_features
        esiid               VARCHAR
        usage_date          DATE
        total_kwh           DOUBLE    - total daily consumption
        peak_kwh            DOUBLE    - kWh during peak hours
        offpeak_kwh         DOUBLE    - kWh during off-peak hours
        peak_ratio          DOUBLE    - peak_kwh / total_kwh (NULL if total = 0)
        max_interval_kwh    DOUBLE    - highest single 15-min value

Usage:
    python scripts/features.py [--db data/smart_meter.duckdb]
                                [--weather data/sample/weather_hourly_clean.csv]
                                [--reset]

Options:
    --db       DuckDB file path (default: data/smart_meter.duckdb)
    --weather  Hourly weather CSV with columns datetime,temp_c
               (default: data/sample/weather_hourly_clean.csv)
    --reset    Drop and recreate silver tables before loading
"""

import argparse
import duckdb
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PEAK_START_HOUR  = 6    # 06:00 inclusive
PEAK_END_HOUR    = 21   # 21:00 exclusive  →  hours 6-20 are on-peak (6am–9pm)
AC_PROXY_TEMP_C  = 23.0 # °C threshold for AC proxy flag


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

DDL_SILVER_SCHEMA = "CREATE SCHEMA IF NOT EXISTS silver"

DDL_INTERVAL_FEATURES = """
CREATE TABLE IF NOT EXISTS silver.interval_features (
    esiid               VARCHAR,
    interval_start_dt   TIMESTAMP,
    usage_kwh           DOUBLE,
    hour_of_day         INTEGER,
    day_of_week         INTEGER,
    is_weekend          BOOLEAN,
    is_peak             BOOLEAN,
    temp_c              DOUBLE,
    ac_proxy            BOOLEAN
)
"""

DDL_DAILY_FEATURES = """
CREATE TABLE IF NOT EXISTS silver.daily_features (
    esiid               VARCHAR,
    usage_date          DATE,
    total_kwh           DOUBLE,
    peak_kwh            DOUBLE,
    offpeak_kwh         DOUBLE,
    peak_ratio          DOUBLE,
    max_interval_kwh    DOUBLE
)
"""

DDL_DROP_INTERVAL = "DROP TABLE IF EXISTS silver.interval_features"
DDL_DROP_DAILY    = "DROP TABLE IF EXISTS silver.daily_features"


# ---------------------------------------------------------------------------
# Feature builders
# ---------------------------------------------------------------------------

def build_interval_features(intervals: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich each 15-min interval with time attributes and a weather join.
    Returns one row per interval.
    """
    df = intervals.copy()

    df["hour_of_day"] = df["interval_start_dt"].dt.hour
    df["day_of_week"] = df["interval_start_dt"].dt.dayofweek   # 0=Mon, 6=Sun
    df["is_weekend"]  = df["day_of_week"] >= 5
    df["is_peak"]     = (
        (df["hour_of_day"] >= PEAK_START_HOUR)
        & (df["hour_of_day"] < PEAK_END_HOUR)
    )

    # Join weather on the top of the hour
    df["hour_dt"] = df["interval_start_dt"].dt.floor("h")
    df = df.merge(weather, on="hour_dt", how="left")
    df["ac_proxy"] = df["temp_c"] > AC_PROXY_TEMP_C

    return df[[
        "esiid", "interval_start_dt", "usage_kwh",
        "hour_of_day", "day_of_week", "is_weekend", "is_peak",
        "temp_c", "ac_proxy",
    ]]


def build_daily_features(interval_features: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate interval_features to one row per (esiid, usage_date).
    """
    df = interval_features.copy()
    df["usage_date"] = df["interval_start_dt"].dt.date

    df["kwh_peak"]    = df["usage_kwh"].where(df["is_peak"], 0.0)
    df["kwh_offpeak"] = df["usage_kwh"].where(~df["is_peak"], 0.0)

    daily = (
        df.groupby(["esiid", "usage_date"])
        .agg(
            total_kwh=("usage_kwh", "sum"),
            peak_kwh=("kwh_peak", "sum"),
            offpeak_kwh=("kwh_offpeak", "sum"),
            max_interval_kwh=("usage_kwh", "max"),
        )
        .reset_index()
    )

    daily["peak_ratio"] = np.where(
        daily["total_kwh"] > 0,
        daily["peak_kwh"] / daily["total_kwh"],
        np.nan,
    )

    return daily[[
        "esiid", "usage_date",
        "total_kwh", "peak_kwh", "offpeak_kwh", "peak_ratio", "max_interval_kwh",
    ]]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_to_silver(
    con: duckdb.DuckDBPyConnection,
    interval_df: pd.DataFrame,
    daily_df: pd.DataFrame,
) -> tuple[int, int]:
    """Insert DataFrames into silver tables. Returns (interval_rows, daily_rows) inserted."""
    # Skip dates already loaded (idempotent)
    loaded_dates = set(
        row[0] for row in con.execute(
            "SELECT DISTINCT CAST(interval_start_dt AS DATE) FROM silver.interval_features"
        ).fetchall()
    )

    new_intervals = interval_df[
        ~interval_df["interval_start_dt"].dt.date.isin(loaded_dates)
    ]
    new_daily = daily_df[
        ~pd.to_datetime(daily_df["usage_date"]).dt.date.isin(loaded_dates)
    ]

    if len(new_intervals) > 0:
        con.execute("INSERT INTO silver.interval_features SELECT * FROM new_intervals")
    if len(new_daily) > 0:
        con.execute("INSERT INTO silver.daily_features SELECT * FROM new_daily")

    return len(new_intervals), len(new_daily)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 2: build silver feature tables from bronze intervals"
    )
    parser.add_argument("--db",      default="data/smart_meter.duckdb",
                        help="DuckDB file path (default: data/smart_meter.duckdb)")
    parser.add_argument("--weather", default="data/sample/weather_hourly_clean.csv",
                        help="Hourly weather CSV (default: data/sample/weather_hourly_clean.csv)")
    parser.add_argument("--reset",   action="store_true",
                        help="Drop and recreate silver tables before loading")
    args = parser.parse_args()

    db_path      = Path(args.db)
    weather_path = Path(args.weather)

    # Load weather
    weather = pd.read_csv(weather_path, parse_dates=["datetime"])
    weather = weather.rename(columns={"datetime": "hour_dt"})
    weather["hour_dt"] = weather["hour_dt"].dt.floor("h")
    weather = weather[["hour_dt", "temp_c"]].drop_duplicates("hour_dt")

    print(f"Weather: {len(weather)} hourly records  "
          f"({weather['temp_c'].min():.1f}–{weather['temp_c'].max():.1f} °C)")

    # Connect and optionally reset
    con = duckdb.connect(str(db_path))
    con.execute(DDL_SILVER_SCHEMA)

    if args.reset:
        con.execute(DDL_DROP_INTERVAL)
        con.execute(DDL_DROP_DAILY)
        print("Silver tables dropped and will be recreated.")

    con.execute(DDL_INTERVAL_FEATURES)
    con.execute(DDL_DAILY_FEATURES)

    # Load bronze intervals (consumption only)
    intervals = con.execute("""
        SELECT esiid, usage_date, interval_start_dt, usage_kwh
        FROM raw.meter_intervals
        WHERE flow_direction = 'Consumption'
        ORDER BY esiid, interval_start_dt
    """).fetchdf()

    print(f"Bronze: {len(intervals):,} intervals across "
          f"{intervals['usage_date'].nunique()} days")

    # Build features
    print("Building interval features...")
    interval_features = build_interval_features(intervals, weather)

    missing_weather = interval_features["temp_c"].isna().sum()
    if missing_weather:
        print(f"  WARN: {missing_weather} intervals have no weather match")

    print("Building daily features...")
    daily_features = build_daily_features(interval_features)

    # Write to silver
    n_intervals, n_daily = load_to_silver(con, interval_features, daily_features)
    con.close()

    print(f"\nFeature engineering complete.")
    print(f"  Inserted : {n_intervals:,} interval rows, {n_daily} daily rows")
    print(f"  DB       : {db_path}")
    print(f"\nSample daily_features:")
    print(daily_features.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
