# smart-meter-nilm

Portfolio complement to [ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker).

`ercot-plan-ranker` simulates and ranks electricity plans against a usage profile.
This project builds the other side: it takes real smart meter interval data and
identifies which appliances are responsible for which portion of the total load.

The technique is called NILM (Non-Intrusive Load Monitoring) - disaggregating
individual appliance signatures from the single aggregate signal at the meter,
with no per-device sensors required.

This repo uses sample data only. No personal usage data is published.
If you want to run it against your own meter data, see the Input format section below.

---

## How the two projects connect

```
smart-meter-nilm                         ercot-plan-ranker
------------------------                 -------------------------
Raw smart meter intervals                Plan rate structures (EFL)
        |                                         |
        v                                         |
  Feature engineering                             |
  (load shape, duty cycles,                       |
   on/off events, daily patterns)                 |
        |                                         |
        v                                         |
  ML disaggregation                               |
  (appliance classification                       |
   + kWh attribution)                             |
        |                                         |
        v                                         v
Per-appliance usage profile ------>  Cost simulation per plan
                                                  |
                                                  v
                                          Ranked plan output
```

---

## Input format

The project expects the standard ERCOT smart meter export (CSV), available from
your utility portal (Oncor, AEP, etc.) under "My Usage" or "Green Button Download".

```
ESIID,USAGE_DATE,REVISION_DATE,USAGE_START_TIME,USAGE_END_TIME,USAGE_KWH,ESTIMATED_ACTUAL,CONSUMPTION_SURPLUSGENERATION
'1234567890,05/06/2026,05/07/2026 07:40:29, 00:00, 00:15,0.859,A,Consumption
'1234567890,05/06/2026,05/07/2026 07:40:29, 00:15, 00:30,0.151,A,Consumption
'1234567890,05/06/2026,05/07/2026 07:40:29, 00:30, 00:45,0.168,A,Consumption
```

One row per 15-minute interval. The pipeline handles the leading apostrophe on
ESIID, whitespace in time columns, and mixed date formats.

Green Button XML is not supported in this repo. If you need XML ingestion, fork
and extend the ingest layer.

---

## What this project implements

### Phase 1 - Ingestion `scripts/ingest.py` ✓
- Walks `data/sample/YYYY/MM/YYYYMMDD.csv` for ERCOT interval CSVs
- Parses ERCOT format: strips leading apostrophe on ESIID, trims whitespace in time columns
- Validates each file: 96 intervals per day, no null kWh, no negatives, no duplicates
- Loads into DuckDB `raw.meter_intervals` (bronze layer) with source file lineage
- Supports `--reset`, `--data-dir`, `--db`; skips already-loaded files (idempotent)

### Phase 2 - Feature engineering `scripts/features.py` ✓
- Enriches each 15-min interval with `hour_of_day`, `day_of_week`, `is_weekend`, `is_peak` (6am–9pm)
- Joins hourly weather (`temp_c`) and derives `ac_proxy` flag (temp_c > 23°C)
- Stores enriched intervals in DuckDB `silver.interval_features`
- Aggregates to daily: `total_kwh`, `peak_kwh`, `offpeak_kwh`, `peak_ratio`, `max_interval_kwh`
- Stores daily aggregates in DuckDB `silver.daily_features`
- Supports `--reset`, `--weather`, `--db`; idempotent

### Phase 3 - ML disaggregation *(in progress)*
- Train a scikit-learn classifier on labeled public dataset (UK-DALE or REFIT)
- Apply model to sample data to attribute kWh by appliance category
- Categories: HVAC, water heater, washer/dryer, EV charger, always-on baseline
- Store per-appliance summary (gold layer)

### Phase 4 - Output
- Export usage profile compatible with `ercot-plan-ranker` input format
- Streamlit dashboard: appliance breakdown, time-of-use heatmap

---

## Stack

| Layer | Tool |
|---|---|
| Storage | DuckDB, Parquet |
| Ingestion | Python, pandas |
| Transformation | dbt Core |
| ML | scikit-learn |
| Orchestration | Airflow (reads and loads the sample dataset) |
| Visualization | Streamlit |

---

## Training data

The ML model is trained on publicly available labeled datasets where both the
total load and individual appliance sub-meters are recorded simultaneously.
This labeled ground truth does not exist in a standard smart meter export.

| Dataset | Description |
|---|---|
| [UK-DALE](https://jack-kelly.com/data/) | UK household dataset with appliance-level sub-metering |
| [REFIT](https://www.refitsmarthomes.org/datasets/) | 20 UK households, appliance labels, CSV format |

---

## Repo structure

```
smart-meter-nilm/
├── data/
│   ├── raw/                        # meter exports (gitignored)
│   └── sample/                     # synthetic ERCOT-format CSVs + weather
│       ├── YYYY/MM/YYYYMMDD.csv    # generated by scripts/generate_sample.py
│       ├── weather_hourly_clean.csv
│       └── weather_raw_hourly.csv
├── scripts/
│   ├── ingest.py                   # Phase 1: load intervals → DuckDB bronze
│   ├── features.py                 # Phase 2: feature engineering → DuckDB silver
│   ├── generate_sample.py          # generate synthetic ERCOT CSVs from weather
│   └── prepare_weather.py          # clean NOAA LCD export → hourly CSV
├── src/                            # importable stubs (populated as phases complete)
│   ├── ingest.py
│   ├── features.py
│   ├── disaggregate.py
│   └── export.py
├── dags/
│   └── load_sample.py              # Airflow DAG: load sample dataset
├── reports/
├── streamlit_app.py
└── README.md
```

---

## Roadmap

- [x] Phase 1: Ingestion pipeline (DuckDB bronze layer)
- [x] Phase 2: Feature engineering + weather enrichment (DuckDB silver layer)
- [ ] Phase 3: NILM model - scikit-learn classifier (gold layer) — *in progress*
- [ ] Phase 4: Export + `ercot-plan-ranker` integration
- [ ] Phase 5: Streamlit dashboard

---

## Related

- **[ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker)** - simulates
  electricity bill costs across ERCOT retail plans and ranks them by scenario.
  Consumes the usage profile produced by this project.
