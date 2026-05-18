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

### Phase 3 - NILM disaggregation `scripts/disaggregate.py` ✓
- Builds a 5-feature matrix per interval: `usage_kwh`, `hour_sin`/`hour_cos` (cyclical),
  `temp_c`, `ac_proxy_int`
- Fits KMeans (k=5) and maps clusters to appliances via centroid rules (greedy, no double-assignment):
  `baseline` → lowest kWh; `hvac` → highest ac_proxy × temp_c; `cooking` → most negative hour_sin
  (≈ evening); `washer`/`dryer` → lower/higher kWh of remaining pair
- Writes `gold.appliance_estimates`: one row per (esiid, date, appliance) with `estimated_kwh`
  and `confidence` (normalized inverse distance to centroid, clamped [0.3, 1.0])
- Validation: attributed totals match `silver.daily_features` exactly; HVAC kWh rises
  monotonically with temperature (5.9 kWh/day at 17°C → 41.6 kWh/day at 30°C)
- Supports `--reset`, `--db`; idempotent
- **Limitation:** with a single ESIID and no labeled data, KMeans finds temperature/time-of-day
  regimes more than clean appliance signatures; mean confidence 0.50–0.66 by appliance

### Phase 4 - Export `scripts/export.py` ✓
- Reads `gold.appliance_estimates` and joins `silver.interval_features` for daily temperature
- Writes `gold.appliance_profile`: one row per (date, appliance) with `estimated_kwh`
  and `pct_of_daily_total`
- Writes `gold.daily_summary`: one row per day with per-appliance kWh columns and
  `avg_temp_c` — compatible with `ercot-plan-ranker` usage profile input
- Default behavior: DuckDB only; add `--csv` to also write `reports/appliance_profile.csv`
  and `reports/daily_summary.csv` from the gold tables
- Supports `--reset` (drops and recreates gold export tables), `--out-dir`, `--db`; idempotent

### Phase 5 - Dashboard `streamlit_app.py` ✓
- Reads directly from DuckDB (gold + silver layers, not from CSV)
- **Tab 1** — Daily total kWh line chart with min/avg/max metrics
- **Tab 2** — Appliance breakdown stacked bar chart + avg attribution table
- **Tab 3** — HVAC load vs temperature scatter plot
- **Tab 4** — Daily load profile: stacked area chart of 15-min intervals for a
  selected day using a rule-based overlay (baseline floor capped at 0.15 kWh,
  HVAC proportional to temperature, morning routine detection 06–08 h,
  cooking at meal hours, washer/dryer by hour and power threshold); variable
  bands are scaled proportionally when their sum exceeds headroom above baseline;
  unclassified load assigned to a separate `other` band (never rolled into
  baseline); outdoor temperature overlaid as a dotted line on a secondary y-axis
  (respects °C / °F sidebar toggle)
- Sidebar: date range filter (applies to all tabs) and °C / °F toggle (display only)
- Run with: `streamlit run streamlit_app.py`

---

## Stack

| Layer | Local pipeline | Databricks notebook |
|---|---|---|
| Storage | DuckDB, Parquet | Delta Lake |
| Ingestion | Python, pandas | PySpark |
| Transformation | dbt Core | Spark SQL |
| ML | scikit-learn KMeans | MLlib KMeans |
| Orchestration | Airflow | Databricks Jobs |
| Visualization | Streamlit | Databricks display() |

---

## Databricks / dual-target pattern

`notebooks/smart_meter_nilm_databricks.py` is a Databricks notebook
(Python format with `# COMMAND ----------` cell separators) that runs
the same pipeline on Spark + Delta Lake instead of DuckDB + pandas.

Same logic, different execution engine — the same cluster mapping rules,
the same 5-feature matrix, and the same bronze → silver → gold medallion
structure. This dual-target approach (local DuckDB for fast iteration,
Databricks for scale) mirrors the pattern used in
[xml-drift-lakehouse](https://github.com/gdcur/xml-drift-lakehouse).

To run on Databricks Free edition:
1. Upload sample data to DBFS:
   ```
   databricks fs cp -r data/sample/ dbfs:/FileStore/smart_meter_nilm/sample/
   databricks fs cp data/sample/weather_hourly_clean.csv dbfs:/FileStore/smart_meter_nilm/weather_hourly_clean.csv
   ```
2. Import `notebooks/smart_meter_nilm_databricks.py` into your workspace
3. Attach to any cluster and click **Run All**

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
│   ├── ingest.py                   # Phase 1: load intervals → DuckDB raw.meter_intervals
│   ├── features.py                 # Phase 2: feature engineering → DuckDB silver.*
│   ├── disaggregate.py             # Phase 3: NILM clustering → DuckDB gold.appliance_estimates
│   ├── export.py                   # Phase 4: export → reports/ CSVs
│   ├── generate_sample.py          # generate synthetic ERCOT CSVs from weather
│   └── prepare_weather.py          # clean NOAA LCD export → hourly CSV
├── dbt/
│   └── models/
│       ├── staging/                # stub: bronze → silver
│       └── mart/                   # stub: silver → gold
├── dags/
│   └── load_sample.py              # Airflow DAG: load sample dataset
├── notebooks/                      # EDA (stub)
├── reports/                        # generated by scripts/export.py
│   ├── appliance_profile.csv
│   └── daily_summary.csv
├── LCD_USW00053907_2025.csv        # raw NOAA LCD station data (source for weather prep)
├── streamlit_app.py                # Phase 5: NILM dashboard
└── README.md
```

---

## Roadmap

- [x] Phase 1: Ingestion pipeline (DuckDB bronze layer)
- [x] Phase 2: Feature engineering + weather enrichment (DuckDB silver layer)
- [x] Phase 3: NILM disaggregation — KMeans clustering + rule-based appliance attribution (gold layer)
- [x] Phase 4: Export — appliance profile + daily summary CSVs
- [x] Phase 5: Streamlit dashboard — daily total, appliance breakdown, HVAC vs temperature
- [x] Phase 6: Airflow orchestration — `dags/load_sample.py`

### Phase 6 - Orchestration `dags/load_sample.py` ✓
- Airflow DAG `smart_meter_nilm_pipeline` with `@daily` schedule, `start_date=2025-05-01`, `catchup=False`
- Four `BashOperator` tasks in a linear chain: `ingest → features → disaggregate → export`
- Each task runs the corresponding `scripts/*.py` from the project root
- Portfolio/local design: orchestrates existing scripts, does not reimplement their logic
- Airflow is not in `requirements.txt` (installation is environment-specific); install with `pip install apache-airflow`
- Trigger manually: `airflow dags trigger smart_meter_nilm_pipeline`

---

## Related

- **[ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker)** - simulates
  electricity bill costs across ERCOT retail plans and ranks them by scenario.
  Consumes the usage profile produced by this project.
