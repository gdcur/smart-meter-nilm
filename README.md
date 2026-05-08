# smart-meter-nilm

> Non-Intrusive Load Monitoring (NILM) pipeline — disaggregate household appliance
> consumption from smart meter interval data, then feed realistic usage profiles
> into [ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker).

---

## Why this project exists

`ercot-plan-ranker` can simulate and rank electricity plans — but only as well as
the usage profile you give it. The sample data bundled there is synthetic: a flat
weekly pattern that doesn't reflect how a real household actually consumes power.

This project solves that. It takes the raw 15-minute interval data a smart meter
actually records, identifies which appliances are responsible for which loads, and
produces a structured usage profile that `ercot-plan-ranker` can consume directly.

The technique is called **NILM — Non-Intrusive Load Monitoring**: you disaggregate
individual appliance signatures from the single aggregate signal at the meter entry
point, with no per-device sensors required.

---

## How the two projects connect

```
smart-meter-nilm                         ercot-plan-ranker
────────────────────────                 ─────────────────────────
Raw smart meter intervals                Plan rate structures (EFL)
        │                                         │
        ▼                                         │
  Feature engineering                             │
  (load shape, duty cycles,                       │
   on/off events, spikes)                         │
        │                                         │
        ▼                                         │
  ML disaggregation                               │
  (appliance classification                       │
   + kWh attribution)                             │
        │                                         │
        ▼                                         ▼
Per-appliance usage profile ────────▶  Cost simulation per plan
                                                  │
                                                  ▼
                                          Ranked plan output
```

Instead of a synthetic sample, `ercot-plan-ranker` receives a real usage profile:
which appliances ran, when, and how much — broken down by time of day and season.

---

## What this project implements

### Phase 1 — Ingestion & storage
- Load 15-minute interval data from a smart meter export (CSV or Green Button XML)
- Store raw intervals in DuckDB as the bronze layer
- Basic validation: gap detection, duplicate removal, unit normalization (kW → kWh)

### Phase 2 — Feature engineering
- Aggregate to hourly and daily load curves
- Extract features: load shape, peak/off-peak ratio, duty cycle patterns, ramp events
- Enrich with weather data (temperature, CDD) to correlate load with conditions
- Store enriched dataset as the silver layer

### Phase 3 — ML disaggregation (NILM)
- Train or apply a classification/clustering model to identify appliance signatures
- Attribute kWh to appliance categories: HVAC, water heater, washer/dryer, EV, always-on baseline
- Produce per-appliance consumption summary as the gold layer

### Phase 4 — Output & integration
- Export a structured usage profile compatible with `ercot-plan-ranker` input format
- Streamlit dashboard: appliance breakdown, time-of-use heatmap, cost attribution

---

## Stack

| Layer | Tool |
|---|---|
| Storage | DuckDB, Parquet |
| Ingestion | Python, pandas / polars |
| Transformation | dbt Core |
| ML | scikit-learn |
| Orchestration | Airflow (Phase 4) |
| Visualization | Streamlit |

---

## Data sources

| Dataset | Description | Format |
|---|---|---|
| Smart meter export | 15-min interval data from utility portal (Oncor, AEP, etc.) | CSV / Green Button XML |
| [UK-DALE](https://jack-kelly.com/data/) | Public labeled appliance dataset for model training | HDF5 / CSV |
| [REFIT](https://www.refitsmarthomes.org/datasets/) | UK household smart meter dataset with appliance labels | CSV |
| Weather | Daily temperature and CDD from NOAA or Open-Meteo | CSV / API |

> UK-DALE and REFIT are used for **model training only** — they provide labeled
> ground truth (total load + individual appliance sub-meters) that does not exist
> in a standard smart meter export.

---

## Repo structure (target)

```
smart-meter-nilm/
├── data/
│   ├── raw/              # meter exports, weather (gitignored)
│   └── sample/           # small synthetic sample for CI/demo runs
├── src/
│   ├── ingest.py         # load and validate interval data
│   ├── features.py       # feature engineering
│   ├── disaggregate.py   # NILM model: train + predict
│   └── export.py         # produce ercot-plan-ranker compatible output
├── dbt/
│   ├── models/
│   │   ├── staging/      # bronze → silver
│   │   └── mart/         # silver → gold (appliance summary)
│   └── profiles.yml
├── notebooks/            # EDA and model experiments
├── reports/              # generated outputs
├── streamlit_app.py      # dashboard
└── README.md
```

---

## Roadmap

- [ ] Phase 1: Ingestion pipeline (DuckDB bronze layer)
- [ ] Phase 2: Feature engineering + weather enrichment (dbt silver layer)
- [ ] Phase 3: NILM model — baseline scikit-learn classifier
- [ ] Phase 4: Gold layer export + `ercot-plan-ranker` integration
- [ ] Phase 5: Streamlit dashboard (appliance breakdown + cost attribution)
- [ ] Phase 6: Airflow orchestration + scheduled refresh

---

## Related

- **[ercot-plan-ranker](https://github.com/gdcur/ercot-plan-ranker)** — consumes
  the usage profile produced here and ranks ERCOT retail electricity plans by
  simulated cost across normal and hot weather scenarios.
