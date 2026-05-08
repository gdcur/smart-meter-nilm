"""
prepare_weather.py

Reads the raw NOAA hourly weather export (as extracted from the full station CSV)
and produces a clean one-row-per-hour file used by generate_sample.py.

Input:  data/sample/weather_raw_hourly.csv
Output: data/sample/weather_hourly_clean.csv

Columns in output:
    datetime  - ISO timestamp, top of each hour (e.g. 2025-05-01 00:00:00)
    temp_c    - dry bulb temperature in Celsius (source units, no conversion)

Usage:
    python scripts/prepare_weather.py
"""

import pandas as pd
from pathlib import Path

INPUT  = Path("data/sample/weather_raw_hourly.csv")
OUTPUT = Path("data/sample/weather_hourly_clean.csv")


def main():
    df = pd.read_csv(INPUT)
    df["DATE"] = pd.to_datetime(df["DATE"])
    df = df[["DATE", "HourlyDryBulbTemperature"]].dropna().sort_values("DATE")

    # Take the last observation of each hour (METAR convention)
    df["hour"] = df["DATE"].dt.floor("h")
    hourly = (
        df.groupby("hour")["HourlyDryBulbTemperature"]
        .last()
        .reset_index()
    )
    hourly.columns = ["datetime", "temp_c"]

    # Reindex to a full continuous hourly range and interpolate any gaps
    full_range = pd.date_range(hourly["datetime"].min(),
                               hourly["datetime"].max(), freq="h")
    hourly = (
        hourly.set_index("datetime")
        .reindex(full_range)
        .interpolate(method="time")
        .reset_index()
    )
    hourly.columns = ["datetime", "temp_c"]
    hourly["temp_c"] = hourly["temp_c"].round(1)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    hourly.to_csv(OUTPUT, index=False)

    print(f"Done. {len(hourly)} hourly records written to {OUTPUT}")
    print(f"  Range : {hourly['datetime'].min()} to {hourly['datetime'].max()}")
    print(f"  Temp  : min={hourly['temp_c'].min()}C  max={hourly['temp_c'].max()}C")


if __name__ == "__main__":
    main()
