"""
generate_sample.py

Generates 60 synthetic ERCOT-format smart meter CSV files (one per day)
driven by real Arlington TX hourly temperature data.

Input:  data/sample/weather_hourly_clean.csv  (output of prepare_weather.py)
Output: data/sample/YYYY/MM/YYYYMMDD.csv      (one file per day)

Appliance signatures simulated:
    - Baseline       : fridge cycling, standby devices (~0.10-0.15 kWh per interval)
    - HVAC           : temperature-driven, ramps above 23C, peaks in afternoon
    - Washer         : 2-3 cycles/day, ~45 min each, daytime only
    - Dryer          : follows washer by 5-10 min, similar duration
    - Oven/cooking   : dinner spike 18:00-20:00, occasional lunch 12:00-13:00
    - Morning        : shower, coffee maker spike 06:30-08:00

ERCOT CSV format (matches real Oncor export):
    ESIID, USAGE_DATE, REVISION_DATE, USAGE_START_TIME, USAGE_END_TIME,
    USAGE_KWH, ESTIMATED_ACTUAL, CONSUMPTION_SURPLUSGENERATION

Usage:
    python scripts/generate_sample.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

WEATHER_FILE = Path("data/sample/weather_hourly_clean.csv")
OUTPUT_ROOT  = Path("data/sample")

FAKE_ESIID   = "'1234567890"   # leading apostrophe matches real ERCOT export
REVISION_LAG = timedelta(days=1, hours=7, minutes=40, seconds=29)

# Reproducible but realistic random seed
RNG = np.random.default_rng(42)


# ---------------------------------------------------------------------------
# Temperature helpers
# ---------------------------------------------------------------------------

def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def ac_load_kw(temp_c: float) -> float:
    """
    Returns A/C load in kW for a given outdoor temperature.
    - Below 22C: no A/C
    - 22-26C:    light A/C (0.0 - 0.8 kW)
    - 26-32C:    moderate (0.8 - 2.0 kW)
    - Above 32C: heavy (2.0 - 3.5 kW)
    """
    if temp_c < 22.0:
        return 0.0
    elif temp_c < 26.0:
        return (temp_c - 22.0) / 4.0 * 0.8
    elif temp_c < 32.0:
        return 0.8 + (temp_c - 26.0) / 6.0 * 1.2
    else:
        return min(2.0 + (temp_c - 32.0) * 0.25, 3.5)


# ---------------------------------------------------------------------------
# Appliance event generators
# ---------------------------------------------------------------------------

def washer_dryer_events(date: pd.Timestamp) -> list[tuple[int, int, float]]:
    """
    Returns list of (start_interval, duration_intervals, kw) tuples.
    Washer: 1.5 kW for 6-8 intervals (45-60 min)
    Dryer:  2.5 kW for 6-8 intervals, starts ~1-2 intervals after washer ends
    Only scheduled between 08:00 and 21:00 (intervals 32-84).
    """
    events = []
    n_cycles = RNG.integers(2, 4)  # 2 or 3 wash cycles per day
    used_intervals = set()

    for _ in range(n_cycles):
        for attempt in range(20):
            start = int(RNG.integers(32, 72))  # 08:00 - 18:00
            w_dur = int(RNG.integers(6, 9))
            if any(i in used_intervals for i in range(start, start + w_dur)):
                continue
            # Washer
            events.append((start, w_dur, 1.5))
            for i in range(start, start + w_dur):
                used_intervals.add(i)
            # Dryer follows
            d_start = start + w_dur + int(RNG.integers(1, 3))
            d_dur = int(RNG.integers(6, 9))
            if d_start + d_dur < 96:
                events.append((d_start, d_dur, 2.5))
                for i in range(d_start, d_start + d_dur):
                    used_intervals.add(i)
            break

    return events


def cooking_events() -> list[tuple[int, int, float]]:
    """
    Returns (start_interval, duration_intervals, kw) for cooking.
    Dinner: 18:00-20:00 (intervals 72-80), 2.0-3.0 kW for 3-6 intervals
    Lunch:  12:00-13:00 (intervals 48-52), 50% chance, lighter 1.5 kW
    """
    events = []
    # Dinner - always
    d_start = int(RNG.integers(72, 78))
    d_dur   = int(RNG.integers(3, 7))
    events.append((d_start, d_dur, float(RNG.uniform(2.0, 3.0))))
    # Lunch - 50% chance
    if RNG.random() > 0.5:
        l_start = int(RNG.integers(48, 52))
        l_dur   = int(RNG.integers(2, 4))
        events.append((l_start, l_dur, 1.5))
    return events


def morning_spike_events() -> list[tuple[int, int, float]]:
    """
    Morning routine: shower + coffee 06:30-08:00 (intervals 26-32)
    Water heater recovery: 1.5-2.0 kW for 2-4 intervals
    Coffee maker: 1.0 kW for 1-2 intervals
    """
    events = []
    start = int(RNG.integers(26, 30))
    events.append((start, int(RNG.integers(2, 5)), float(RNG.uniform(1.5, 2.0))))
    events.append((start + 1, int(RNG.integers(1, 3)), 1.0))
    return events


# ---------------------------------------------------------------------------
# Per-interval load builder
# ---------------------------------------------------------------------------

def build_day_load(date: pd.Timestamp,
                   hourly_temps: pd.Series) -> np.ndarray:
    """
    Returns array of 96 kWh values (one per 15-min interval) for the given date.
    hourly_temps: Series indexed 0-23 with temp_c for each hour of the day.
    """
    load = np.zeros(96)

    # 1. Baseline (fridge cycling + standby)
    baseline = RNG.uniform(0.10, 0.15, size=96)
    load += baseline

    # 2. HVAC - temperature driven, per interval
    for interval in range(96):
        hour = interval // 4
        temp = hourly_temps.iloc[hour] if hour < len(hourly_temps) else hourly_temps.iloc[-1]
        ac_kw = ac_load_kw(float(temp))
        # Add slight randomness (cycling on/off)
        ac_kw *= float(RNG.uniform(0.85, 1.15))
        # Overnight reduction (23:00-07:00)
        if hour < 7 or hour >= 23:
            ac_kw *= 0.3
        load[interval] += ac_kw * 0.25  # kW * 0.25h = kWh

    # 3. Washer/dryer
    for start, dur, kw in washer_dryer_events(date):
        for i in range(start, min(start + dur, 96)):
            load[i] += kw * 0.25 * float(RNG.uniform(0.9, 1.1))

    # 4. Cooking
    for start, dur, kw in cooking_events():
        for i in range(start, min(start + dur, 96)):
            load[i] += kw * 0.25 * float(RNG.uniform(0.9, 1.1))

    # 5. Morning spike
    for start, dur, kw in morning_spike_events():
        for i in range(start, min(start + dur, 96)):
            load[i] += kw * 0.25 * float(RNG.uniform(0.9, 1.1))

    # Round to 3 decimal places (matches real ERCOT files)
    return np.round(load, 3)


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_day_csv(date: pd.Timestamp,
                  load: np.ndarray,
                  output_root: Path) -> None:
    usage_date   = date.strftime("%m/%d/%Y")
    revision_dt  = (date + REVISION_LAG).strftime("%m/%d/%Y %H:%M:%S")

    rows = []
    for i in range(96):
        h_start, m_start = divmod(i * 15, 60)
        h_end,   m_end   = divmod((i + 1) * 15, 60)
        rows.append({
            "ESIID":                          FAKE_ESIID,
            "USAGE_DATE":                     usage_date,
            "REVISION_DATE":                  revision_dt,
            "USAGE_START_TIME":               f" {h_start:02d}:{m_start:02d}",
            "USAGE_END_TIME":                 f" {h_end:02d}:{m_end:02d}",
            "USAGE_KWH":                      load[i],
            "ESTIMATED_ACTUAL":               "A",
            "CONSUMPTION_SURPLUSGENERATION":  "Consumption",
        })

    out_path = output_root / date.strftime("%Y/%m/%Y%m%d.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(rows).to_csv(out_path, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    weather = pd.read_csv(WEATHER_FILE, parse_dates=["datetime"])
    weather = weather.set_index("datetime")

    dates = sorted(weather.index.normalize().unique())
    print(f"Generating {len(dates)} daily files...")

    for date in dates:
        date_ts = pd.Timestamp(date)
        # Hourly temps for this day (24 values)
        day_temps = weather.loc[
            (weather.index >= date_ts) &
            (weather.index < date_ts + pd.Timedelta(days=1)),
            "temp_c"
        ]
        # Reindex to exactly 24 hours
        full_hours = pd.date_range(date_ts, periods=24, freq="h")
        day_temps = day_temps.reindex(full_hours).interpolate(method="time").ffill().bfill()

        load = build_day_load(date_ts, day_temps.reset_index(drop=True))
        write_day_csv(date_ts, load, OUTPUT_ROOT)

    print(f"Done. Files written to {OUTPUT_ROOT}/YYYY/MM/YYYYMMDD.csv")
    print(f"Total daily kWh range across sample:")

    # Quick sanity check
    total_kwh = []
    for date in dates:
        p = OUTPUT_ROOT / pd.Timestamp(date).strftime("%Y/%m/%Y%m%d.csv")
        df = pd.read_csv(p)
        total_kwh.append(df["USAGE_KWH"].sum())
    arr = np.array(total_kwh)
    print(f"  min={arr.min():.1f}  max={arr.max():.1f}  avg={arr.mean():.1f} kWh/day")


if __name__ == "__main__":
    main()
