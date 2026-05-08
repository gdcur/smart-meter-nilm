"""
disaggregate.py

Phase 3 - NILM disaggregation (gold layer).

Reads silver.interval_features and silver.daily_features, clusters 15-min
intervals by load pattern using KMeans, maps each cluster to an appliance
category using centroid rules, and writes per-day per-appliance kWh estimates
to gold.appliance_estimates.

Gold table: gold.appliance_estimates
    esiid           VARCHAR   - meter identifier
    usage_date      DATE
    appliance       VARCHAR   - hvac | dryer | washer | cooking | baseline
    estimated_kwh   DOUBLE    - kWh attributed to this appliance on this day
    confidence      DOUBLE    - mean cluster confidence for this day (0–1)

Approach:
    1. Build 5-feature matrix per interval:
         usage_kwh, hour_sin, hour_cos (cyclical encoding), temp_c, ac_proxy
    2. KMeans with k=5 (one cluster per appliance)
    3. Map clusters to appliances from centroid rules (in priority order):
         baseline → lowest usage_kwh centroid
         hvac     → highest (ac_proxy × temp_c) — most temperature-driven
         cooking  → most negative hour_sin centroid (≈ evening 18–19 h)
         washer   → lower kWh of remaining pair
         dryer    → higher kWh of remaining pair
    4. Confidence per interval = 1 − (dist_to_centroid / max_dist_in_cluster),
       clamped to [MIN_CONFIDENCE, 1.0]
    5. Aggregate to (esiid, date, appliance): sum kWh, mean confidence

Note: with no labeled data this attribution is approximate. The sum of
estimated_kwh across all appliances equals total_kwh from the silver layer
by construction (every interval is assigned to exactly one cluster).

Usage:
    python scripts/disaggregate.py [--db data/smart_meter.duckdb] [--reset]

Options:
    --db     DuckDB file path (default: data/smart_meter.duckdb)
    --reset  Drop and recreate gold table before loading
"""

import argparse
import duckdb
import numpy as np
import pandas as pd
from pathlib import Path
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_CLUSTERS     = 5
RANDOM_STATE   = 42
MIN_CONFIDENCE = 0.3   # floor for per-interval confidence scores


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

DDL_GOLD_SCHEMA = "CREATE SCHEMA IF NOT EXISTS gold"

DDL_GOLD_TABLE = """
CREATE TABLE IF NOT EXISTS gold.appliance_estimates (
    esiid           VARCHAR,
    usage_date      DATE,
    appliance       VARCHAR,
    estimated_kwh   DOUBLE,
    confidence      DOUBLE
)
"""

DDL_DROP_GOLD = "DROP TABLE IF EXISTS gold.appliance_estimates"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_silver(con):
    """Load interval and daily silver tables. Returns (interval_df, daily_df)."""
    intervals = con.execute("""
        SELECT esiid, interval_start_dt, usage_kwh,
               hour_of_day, is_peak, temp_c, ac_proxy
        FROM silver.interval_features
        ORDER BY esiid, interval_start_dt
    """).fetchdf()

    daily = con.execute("""
        SELECT esiid, usage_date, total_kwh
        FROM silver.daily_features
        ORDER BY esiid, usage_date
    """).fetchdf()

    return intervals, daily


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_feature_matrix(df):
    """
    Build the interval-level feature matrix for KMeans.

    Features:
        usage_kwh     - raw interval consumption
        hour_sin      - cyclical hour encoding (avoids 23→0 discontinuity)
        hour_cos      - cyclical hour encoding
        temp_c        - outdoor temperature
        ac_proxy_int  - binary flag: temp > 23°C

    Returns (feature_names, scaler, X_scaled).
    """
    feat = df.copy()
    feat["hour_sin"]     = np.sin(2 * np.pi * feat["hour_of_day"] / 24)
    feat["hour_cos"]     = np.cos(2 * np.pi * feat["hour_of_day"] / 24)
    feat["ac_proxy_int"] = feat["ac_proxy"].astype(int)

    feature_names = ["usage_kwh", "hour_sin", "hour_cos", "temp_c", "ac_proxy_int"]
    X = feat[feature_names].copy()
    X["temp_c"] = X["temp_c"].fillna(X["temp_c"].mean())

    scaler   = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    return feature_names, scaler, X_scaled


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def fit_clusters(X_scaled):
    """Fit KMeans and return (kmeans, labels, per-interval distances to centroid)."""
    kmeans = KMeans(n_clusters=N_CLUSTERS, random_state=RANDOM_STATE, n_init=10)
    labels = kmeans.fit_predict(X_scaled)

    centroids_assigned = kmeans.cluster_centers_[labels]
    distances = np.linalg.norm(X_scaled - centroids_assigned, axis=1)

    return kmeans, labels, distances


def map_clusters(kmeans, scaler, feature_names):
    """
    Assign each cluster to an appliance using centroid rules.

    Priority order (greedy, no double-assignment):
        1. baseline  → lowest usage_kwh centroid
        2. hvac      → highest (ac_proxy_int × temp_c) product
        3. cooking   → most negative hour_sin (≈ evening 18–19 h)
        4. washer    → lower kWh of remaining pair
        5. dryer     → higher kWh of remaining pair

    Returns ({cluster_id: appliance_name}, centroids_df).
    """
    centroids = pd.DataFrame(
        scaler.inverse_transform(kmeans.cluster_centers_),
        columns=feature_names,
    )

    assignment = {}
    remaining  = list(range(N_CLUSTERS))

    # 1. baseline: always-on floor of consumption
    b = centroids.loc[remaining, "usage_kwh"].idxmin()
    assignment[b] = "baseline"
    remaining.remove(b)

    # 2. hvac: most temperature-driven (ac_proxy × temp product)
    hvac_score = centroids.loc[remaining, "ac_proxy_int"] * centroids.loc[remaining, "temp_c"]
    h = hvac_score.idxmax()
    assignment[h] = "hvac"
    remaining.remove(h)

    # 3. cooking: most negative hour_sin (evening peak ≈ 18–19 h)
    c = centroids.loc[remaining, "hour_sin"].idxmin()
    assignment[c] = "cooking"
    remaining.remove(c)

    # 4+5. washer / dryer: distinguished by power level
    r0, r1 = sorted(remaining, key=lambda i: centroids.loc[i, "usage_kwh"])
    assignment[r0] = "washer"
    assignment[r1] = "dryer"

    return assignment, centroids


# ---------------------------------------------------------------------------
# Daily attribution
# ---------------------------------------------------------------------------

def estimate_daily(interval_df, labels, distances, cluster_map):
    """
    Aggregate interval-level assignments to one row per (esiid, date, appliance).

    Confidence per interval = 1 − (dist / max_dist_in_cluster),
    clamped to [MIN_CONFIDENCE, 1.0].
    Confidence per output row = mean of interval confidences.
    """
    df = interval_df.copy()
    df["cluster"]    = labels
    df["appliance"]  = df["cluster"].map(cluster_map)
    df["distance"]   = distances
    df["usage_date"] = df["interval_start_dt"].dt.date

    max_dist = df.groupby("cluster")["distance"].transform("max").clip(lower=1e-9)
    df["confidence"] = (1.0 - df["distance"] / max_dist).clip(lower=MIN_CONFIDENCE)

    gold = (
        df.groupby(["esiid", "usage_date", "appliance"])
        .agg(
            estimated_kwh=("usage_kwh", "sum"),
            confidence=("confidence", "mean"),
        )
        .reset_index()
    )

    return gold[["esiid", "usage_date", "appliance", "estimated_kwh", "confidence"]]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_to_gold(con, gold_df):
    """Insert gold estimates into DuckDB. Skips dates already loaded. Returns rows inserted."""
    loaded_dates = set(
        row[0] for row in con.execute(
            "SELECT DISTINCT usage_date FROM gold.appliance_estimates"
        ).fetchall()
    )

    new_rows = gold_df[
        ~pd.to_datetime(gold_df["usage_date"]).dt.date.isin(loaded_dates)
    ]

    if len(new_rows) > 0:
        con.execute("INSERT INTO gold.appliance_estimates SELECT * FROM new_rows")

    return len(new_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 3: NILM disaggregation — attribute kWh per appliance per day"
    )
    parser.add_argument("--db",    default="data/smart_meter.duckdb",
                        help="DuckDB file path (default: data/smart_meter.duckdb)")
    parser.add_argument("--reset", action="store_true",
                        help="Drop and recreate gold table before loading")
    args = parser.parse_args()

    db_path = Path(args.db)

    con = duckdb.connect(str(db_path))
    con.execute(DDL_GOLD_SCHEMA)

    if args.reset:
        con.execute(DDL_DROP_GOLD)
        print("Gold table dropped and will be recreated.")

    con.execute(DDL_GOLD_TABLE)

    # Load silver
    print("Loading silver layer...")
    intervals, daily = load_silver(con)
    n_days = intervals["interval_start_dt"].dt.date.nunique()
    print(f"  {len(intervals):,} intervals  |  {n_days} days  |  "
          f"{intervals['esiid'].nunique()} meter(s)")

    # Feature matrix
    print("Building feature matrix...")
    feature_names, scaler, X_scaled = build_feature_matrix(intervals)

    # Cluster
    print(f"Fitting KMeans (k={N_CLUSTERS})...")
    kmeans, labels, distances = fit_clusters(X_scaled)
    cluster_map, centroids = map_clusters(kmeans, scaler, feature_names)

    print(f"\n  Cluster → appliance mapping:")
    print(f"  {'C':>2}  {'appliance':10}  {'n':>6}  {'kwh':>6}  {'hour':>5}  {'temp':>5}  {'ac%':>5}")
    print(f"  {'-' * 52}")
    for cid in range(N_CLUSTERS):
        mask   = labels == cid
        app    = cluster_map[cid]
        n      = mask.sum()
        kwh    = intervals.loc[mask, "usage_kwh"].mean()
        hour   = intervals.loc[mask, "hour_of_day"].mean()
        temp   = intervals.loc[mask, "temp_c"].mean()
        ac_pct = intervals.loc[mask, "ac_proxy"].mean() * 100
        print(f"  C{cid}  {app:10}  {n:6,}  {kwh:6.3f}  {hour:5.1f}  {temp:5.1f}  {ac_pct:5.1f}%")

    # Daily attribution
    print("\nEstimating daily appliance kWh...")
    gold = estimate_daily(intervals, labels, distances, cluster_map)
    print(f"  {len(gold):,} rows  ({N_CLUSTERS} appliances × {n_days} days)")

    # Per-appliance summary
    avg_kwh = gold.groupby("appliance")["estimated_kwh"].mean().sort_values(ascending=False)
    total_avg = daily["total_kwh"].mean()
    print(f"\n  Avg kWh/day per appliance (total = {total_avg:.1f} kWh):")
    for app, kwh in avg_kwh.items():
        bar = "█" * int(kwh / total_avg * 40)
        print(f"    {app:10}  {kwh:6.2f} kWh  {kwh/total_avg*100:5.1f}%  {bar}")

    avg_conf = gold.groupby("appliance")["confidence"].mean().sort_values(ascending=False)
    print(f"\n  Mean confidence by appliance:")
    for app, conf in avg_conf.items():
        print(f"    {app:10}  {conf:.3f}")

    # Write
    n_inserted = load_to_gold(con, gold)
    con.close()

    print(f"\nDisaggregation complete.")
    print(f"  Inserted : {n_inserted:,} rows into gold.appliance_estimates")
    print(f"  DB       : {db_path}")

    print(f"\nSample output (first day):")
    first_date = gold["usage_date"].min()
    sample = gold[gold["usage_date"] == first_date].sort_values("estimated_kwh", ascending=False)
    print(sample.to_string(index=False))


if __name__ == "__main__":
    main()
