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

### Phase 1 - Ingestion
- Load and validate ERCOT interval CSV
- Handle ESIID formatting, timestamp parsing, gap detection
- Store raw intervals in DuckDB (bronze layer)

### Phase 2 - Feature engineering
- Aggregate to hourly and daily load curves
- Extract features: load shape, peak/off-peak ratio, duty cycle patterns, ramp events
- Enrich with weather data (temperature, CDD) via Open-Meteo
- Store enriched dataset in DuckDB (silver layer)

### Phase 3 - ML disaggregation
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
│   └── sample/           # synthetic ERCOT-format CSV for demo runs
├── src/
│   ├── ingest.py         # load and validate interval CSV
│   ├── features.py       # feature engineering
│   ├── disaggregate.py   # NILM model: train + predict
│   └── export.py         # produce ercot-plan-ranker compatible output
├── dbt/
│   ├── models/
│   │   ├── staging/      # bronze to silver
│   │   └── mart/         # silver to gold (appliance summary)
│   └── profiles.yml
├── dags/
│   └── load_sample.py    # Airflow DAG: reads and loads the sample dataset
├── notebooks/            # EDA and model experiments
├── reports/              # generated outputs
├── streamlit_app.py
└── README.md
```

---

## Roadmap

- [ ] Phase 1: Ingestion pipeline (DuckDB bronze layer)
- [ ] Phase 2: Feature engineering + weather enrichment (dbt silver layer)
- [ ] Phase 3: NILM model - scikit-learn classifier (gold layer)
- [ ] Phase 4: Export + `ercot-plan-ranker` integration
- [ ] Phase 5: Streamlit dashboard

---

## Related

- **[ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker)** - simulates
  electricity bill costs across ERCOT retail plans and ranks them by scenario.
  Consumes the usage profile produced by this project.
