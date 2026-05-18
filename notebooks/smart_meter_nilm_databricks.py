# Databricks notebook source
"""
smart_meter_nilm_databricks.py

Databricks / Spark equivalent of the local smart-meter-nilm pipeline.

Same logic as the scripts/ pipeline; different execution engine:
    Local pipeline  : pandas + DuckDB + scikit-learn
    This notebook   : PySpark + Delta Lake + MLlib KMeans

Designed for Databricks Free edition (single-node cluster, Unity Catalog
or Hive metastore, no MLflow experiment server required).

To import:
    1. In your Databricks workspace click Import → File
    2. Upload this .py file — Databricks detects the # COMMAND ----------
       separators and renders it as a multi-cell notebook
    3. Attach to any running cluster and click Run All

Data expected on DBFS:
    /FileStore/smart_meter_nilm/sample/YYYY/MM/YYYYMMDD.csv
    /FileStore/smart_meter_nilm/weather_hourly_clean.csv

Upload with:
    databricks fs cp -r data/sample/ dbfs:/FileStore/smart_meter_nilm/sample/
    databricks fs cp data/sample/weather_hourly_clean.csv \
        dbfs:/FileStore/smart_meter_nilm/weather_hourly_clean.csv
"""

# COMMAND ----------

# MAGIC %md
# MAGIC # smart-meter-nilm — Databricks Edition
# MAGIC
# MAGIC This notebook replicates the local `smart-meter-nilm` pipeline using
# MAGIC **Spark + Delta Lake + MLlib** instead of pandas + DuckDB + scikit-learn.
# MAGIC The pipeline logic is identical — only the execution engine changes.
# MAGIC
# MAGIC **Stages**
# MAGIC | Cell | Stage | Local equivalent |
# MAGIC |---|---|---|
# MAGIC | 3 | Load raw CSVs | `scripts/ingest.py` |
# MAGIC | 4 | Bronze Delta table | `raw.meter_intervals` |
# MAGIC | 5 | Silver feature engineering | `scripts/features.py` |
# MAGIC | 6 | MLlib KMeans disaggregation | `scripts/disaggregate.py` |
# MAGIC | 7 | Results summary | `scripts/export.py` |
# MAGIC
# MAGIC **Data source:** synthetic ERCOT-format CSVs generated from real Arlington TX
# MAGIC temperature data, covering May–June 2025 (60+ days, 96 intervals/day).

# COMMAND ----------

# Configuration
# In production these would come from a config file, job parameters, or
# Databricks Widgets (dbutils.widgets.get).

ESIID           = "1234567890"
DATE_START      = "2025-05-01"
DATE_END        = "2025-06-30"
AC_THRESHOLD_C  = 23.0
PEAK_START_HOUR = 6    # inclusive
PEAK_END_HOUR   = 21   # exclusive → hours 6-20 are on-peak

# DBFS paths — adjust if you mount an external bucket instead
INTERVALS_ROOT  = "dbfs:/FileStore/smart_meter_nilm/sample"
WEATHER_PATH    = "dbfs:/FileStore/smart_meter_nilm/weather_hourly_clean.csv"

# Delta catalog / schema
CATALOG  = "hive_metastore"   # swap for your Unity Catalog name if enabled
DATABASE = "smart_meter"

spark.sql(f"CREATE DATABASE IF NOT EXISTS {CATALOG}.{DATABASE}")

# COMMAND ----------

# Cell 3 — Load raw intervals
# Reads all ERCOT-format CSVs under INTERVALS_ROOT recursively.
# Handles:
#   - Leading apostrophe on ESIID  ('1234567890 → 1234567890)
#   - Leading whitespace in USAGE_START_TIME / USAGE_END_TIME
#   - MM/DD/YYYY date format

from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType

raw = (
    spark.read.option("header", "true")
         .option("recursiveFileLookup", "true")
         .csv(INTERVALS_ROOT)
)

# Normalize column names (strip whitespace, upper-case)
for col in raw.columns:
    raw = raw.withColumnRenamed(col, col.strip().upper())

raw = (
    raw
    # Strip leading apostrophe and whitespace from ESIID
    .withColumn("esiid",
        F.regexp_replace(F.trim(F.col("ESIID")), r"^'", ""))
    # Parse dates
    .withColumn("usage_date",
        F.to_date(F.col("USAGE_DATE"), "MM/dd/yyyy"))
    .withColumn("revision_date",
        F.to_timestamp(F.col("REVISION_DATE"), "MM/dd/yyyy HH:mm:ss"))
    # Strip whitespace from time columns and build interval timestamp
    .withColumn("interval_start", F.trim(F.col("USAGE_START_TIME")))
    .withColumn("interval_end",   F.trim(F.col("USAGE_END_TIME")))
    .withColumn("interval_start_dt",
        F.to_timestamp(
            F.concat(F.col("usage_date").cast("string"),
                     F.lit(" "),
                     F.trim(F.col("USAGE_START_TIME"))),
            "yyyy-MM-dd HH:mm"))
    .withColumn("usage_kwh",     F.col("USAGE_KWH").cast(DoubleType()))
    .withColumnRenamed("ESTIMATED_ACTUAL",             "estimated_actual")
    .withColumnRenamed("CONSUMPTION_SURPLUSGENERATION","flow_direction")
    .filter(F.col("flow_direction") == "Consumption")
    .select("esiid", "usage_date", "revision_date",
            "interval_start", "interval_end", "interval_start_dt",
            "usage_kwh", "estimated_actual", "flow_direction")
)

display(raw.limit(5))

# COMMAND ----------

# Cell 4 — Bronze layer: write to Delta
# Partitioned by usage_date to match the local DuckDB bronze table design.

(
    raw.write
       .format("delta")
       .mode("overwrite")
       .partitionBy("usage_date")
       .saveAsTable(f"{CATALOG}.{DATABASE}.bronze_meter_intervals")
)

bronze = spark.table(f"{CATALOG}.{DATABASE}.bronze_meter_intervals")
print(f"Bronze rows: {bronze.count():,}")

# COMMAND ----------

# Cell 5 — Silver layer: feature engineering
# Replicates scripts/features.py:
#   hour_of_day, day_of_week, is_weekend, is_peak
#   weather join on date+hour → temp_c, ac_proxy

import math

# Load weather and floor to the top of each hour
weather = (
    spark.read.option("header", "true")
         .option("inferSchema", "true")
         .csv(WEATHER_PATH)
    .withColumn("hour_dt",
        F.date_trunc("hour", F.col("datetime").cast("timestamp")))
    .select("hour_dt", F.col("temp_c").cast(DoubleType()))
    .dropDuplicates(["hour_dt"])
)

# Build interval features
intervals = spark.table(f"{CATALOG}.{DATABASE}.bronze_meter_intervals")

silver = (
    intervals
    .withColumn("hour_of_day",  F.hour("interval_start_dt"))
    .withColumn("day_of_week",  F.dayofweek("interval_start_dt") - 2)  # 0=Mon … 6=Sun
    .withColumn("is_weekend",
        F.col("day_of_week").isin([5, 6]))
    .withColumn("is_peak",
        (F.col("hour_of_day") >= PEAK_START_HOUR) &
        (F.col("hour_of_day") < PEAK_END_HOUR))
    # Weather join key: floor interval timestamp to the hour
    .withColumn("hour_dt", F.date_trunc("hour", "interval_start_dt"))
    .join(weather, on="hour_dt", how="left")
    .withColumn("ac_proxy",
        F.col("temp_c") > F.lit(AC_THRESHOLD_C))
    .drop("hour_dt")
)

(
    silver.write
          .format("delta")
          .mode("overwrite")
          .partitionBy("usage_date")
          .saveAsTable(f"{CATALOG}.{DATABASE}.silver_interval_features")
)

display(spark.table(f"{CATALOG}.{DATABASE}.silver_interval_features").limit(5))

# COMMAND ----------

# Cell 6 — MLlib KMeans disaggregation
# Replicates scripts/disaggregate.py using MLlib instead of scikit-learn.
#
# Features: usage_kwh, hour_sin, hour_cos, temp_c, ac_proxy_int  (same 5 as local)
# Cluster→appliance mapping rules (greedy, same priority as disaggregate.py):
#   baseline  → lowest usage_kwh centroid
#   hvac      → highest (ac_proxy_int × temp_c) product
#   cooking   → most negative hour_sin centroid (≈ evening 18–19 h)
#   washer    → lower kWh of the remaining pair
#   dryer     → higher kWh of the remaining pair

from pyspark.ml.feature import VectorAssembler, StandardScaler
from pyspark.ml.clustering import KMeans
from pyspark.ml import Pipeline

N_CLUSTERS   = 5
RANDOM_SEED  = 42
MIN_CONF     = 0.3

feat_df = (
    spark.table(f"{CATALOG}.{DATABASE}.silver_interval_features")
    .withColumn("hour_sin",
        F.sin(F.col("hour_of_day") * F.lit(2 * math.pi / 24)))
    .withColumn("hour_cos",
        F.cos(F.col("hour_of_day") * F.lit(2 * math.pi / 24)))
    .withColumn("ac_proxy_int", F.col("ac_proxy").cast("int"))
    # Fill any missing temp_c with the column mean
    .fillna({"temp_c": 0.0})   # placeholder; mean fill done below
)

# Mean-fill temp_c
mean_temp = feat_df.agg(F.mean("temp_c")).collect()[0][0]
feat_df = feat_df.fillna({"temp_c": mean_temp})

feature_cols = ["usage_kwh", "hour_sin", "hour_cos", "temp_c", "ac_proxy_int"]

assembler = VectorAssembler(inputCols=feature_cols, outputCol="raw_features")
scaler    = StandardScaler(inputCol="raw_features", outputCol="features",
                           withMean=True, withStd=True)
kmeans    = KMeans(featuresCol="features", predictionCol="cluster",
                   k=N_CLUSTERS, seed=RANDOM_SEED, maxIter=20)

pipeline = Pipeline(stages=[assembler, scaler, kmeans])
model    = pipeline.fit(feat_df)
clustered = model.transform(feat_df)

# --- Cluster → appliance mapping ---
# Extract centroids in original (unscaled) feature space via per-cluster means
centroid_df = (
    clustered
    .groupBy("cluster")
    .agg(
        F.mean("usage_kwh").alias("usage_kwh"),
        F.mean("hour_sin").alias("hour_sin"),
        F.mean("temp_c").alias("temp_c"),
        F.mean("ac_proxy_int").alias("ac_proxy_int"),
    )
    .orderBy("cluster")
    .toPandas()
    .set_index("cluster")
)

assignment = {}
remaining  = list(centroid_df.index)

# 1. baseline: lowest usage_kwh
b = centroid_df.loc[remaining, "usage_kwh"].idxmin()
assignment[b] = "baseline"; remaining.remove(b)

# 2. hvac: highest ac_proxy × temp product
hvac_score = centroid_df.loc[remaining, "ac_proxy_int"] * centroid_df.loc[remaining, "temp_c"]
h = hvac_score.idxmax()
assignment[h] = "hvac"; remaining.remove(h)

# 3. cooking: most negative hour_sin (≈ evening)
c = centroid_df.loc[remaining, "hour_sin"].idxmin()
assignment[c] = "cooking"; remaining.remove(c)

# 4+5. washer / dryer by power level
r0, r1 = sorted(remaining, key=lambda i: centroid_df.loc[i, "usage_kwh"])
assignment[r0] = "washer"
assignment[r1] = "dryer"

print("Cluster → appliance mapping:")
print(f"  {'C':>2}  {'appliance':10}  {'kWh':>6}  {'hour_sin':>8}  {'temp_c':>6}  {'ac%':>5}")
print(f"  {'-' * 50}")
for cid, app in sorted(assignment.items()):
    row = centroid_df.loc[cid]
    print(f"  C{cid}  {app:10}  {row['usage_kwh']:6.3f}  "
          f"{row['hour_sin']:8.3f}  {row['temp_c']:6.1f}  {row['ac_proxy_int']*100:5.1f}%")

# --- Map cluster labels to appliances and compute confidence ---
from pyspark.sql.functions import udf
from pyspark.sql.types import StringType

mapping_bc  = spark.sparkContext.broadcast(assignment)

@udf(StringType())
def cluster_to_appliance(cid):
    return mapping_bc.value.get(int(cid), "unknown")

# Confidence: 1 − (dist / max_dist_in_cluster), clamped to [MIN_CONF, 1.0]
# Approximate distance as Euclidean distance in raw feature space
from pyspark.ml.linalg import Vectors
from pyspark.sql.types import DoubleType as DT

km_model = model.stages[-1]

@udf(DT())
def dist_to_centroid(features, cluster):
    # features is a DenseVector; compute norm vs centroid
    c_vec = km_model.clusterCenters()[int(cluster)]
    diff  = [a - b for a, b in zip(features.toArray(), c_vec)]
    return float(sum(d * d for d in diff) ** 0.5)

clustered = (
    clustered
    .withColumn("appliance",  cluster_to_appliance(F.col("cluster")))
    .withColumn("distance",   dist_to_centroid(F.col("features"), F.col("cluster")))
)

max_dist = (
    clustered
    .groupBy("cluster")
    .agg(F.max("distance").alias("max_dist"))
)
clustered = clustered.join(max_dist, on="cluster", how="left")
clustered = clustered.withColumn(
    "confidence",
    F.greatest(F.lit(MIN_CONF),
               F.lit(1.0) - F.col("distance") / F.greatest(F.col("max_dist"), F.lit(1e-9)))
)

# --- Aggregate to (esiid, usage_date, appliance) ---
gold = (
    clustered
    .groupBy("esiid", "usage_date", "appliance")
    .agg(
        F.round(F.sum("usage_kwh"),    4).alias("estimated_kwh"),
        F.round(F.mean("confidence"),  4).alias("confidence"),
    )
    .orderBy("usage_date", "appliance")
)

(
    gold.write
        .format("delta")
        .mode("overwrite")
        .partitionBy("usage_date")
        .saveAsTable(f"{CATALOG}.{DATABASE}.gold_appliance_estimates")
)

print(f"\nGold rows written: {gold.count():,}")

# COMMAND ----------

# Cell 7 — Results summary

summary = spark.sql(f"""
    SELECT
        usage_date,
        ROUND(SUM(estimated_kwh), 2)                                                        AS total_kwh,
        ROUND(SUM(CASE WHEN appliance = 'hvac'     THEN estimated_kwh ELSE 0 END), 2)       AS hvac_kwh,
        ROUND(SUM(CASE WHEN appliance = 'washer'   THEN estimated_kwh ELSE 0 END), 2)       AS washer_kwh,
        ROUND(SUM(CASE WHEN appliance = 'dryer'    THEN estimated_kwh ELSE 0 END), 2)       AS dryer_kwh,
        ROUND(SUM(CASE WHEN appliance = 'cooking'  THEN estimated_kwh ELSE 0 END), 2)       AS cooking_kwh,
        ROUND(SUM(CASE WHEN appliance = 'baseline' THEN estimated_kwh ELSE 0 END), 2)       AS baseline_kwh,
        ROUND(AVG(confidence), 3)                                                            AS avg_confidence
    FROM {CATALOG}.{DATABASE}.gold_appliance_estimates
    GROUP BY usage_date
    ORDER BY usage_date
""")

display(summary)

# COMMAND ----------

# MAGIC %md
# MAGIC ## MLflow Tracking
# MAGIC
# MAGIC In a production Databricks setup you would log the KMeans run to MLflow so
# MAGIC every retrain is reproducible and comparable:
# MAGIC
# MAGIC ```python
# MAGIC import mlflow
# MAGIC import mlflow.spark
# MAGIC
# MAGIC with mlflow.start_run(run_name="nilm_kmeans"):
# MAGIC     # Parameters
# MAGIC     mlflow.log_params({
# MAGIC         "n_clusters":    N_CLUSTERS,
# MAGIC         "max_iter":      20,
# MAGIC         "random_seed":   RANDOM_SEED,
# MAGIC         "ac_threshold":  AC_THRESHOLD_C,
# MAGIC         "feature_cols":  ",".join(feature_cols),
# MAGIC     })
# MAGIC
# MAGIC     # Metrics — cluster inertia and per-appliance attribution share
# MAGIC     km_model = model.stages[-1]
# MAGIC     mlflow.log_metric("inertia", km_model.summary.trainingCost)
# MAGIC
# MAGIC     avg_kwh = (
# MAGIC         gold.groupBy("appliance")
# MAGIC             .agg(F.mean("estimated_kwh").alias("avg_kwh"))
# MAGIC             .toPandas()
# MAGIC             .set_index("appliance")["avg_kwh"]
# MAGIC     )
# MAGIC     total = avg_kwh.sum()
# MAGIC     for appliance, kwh in avg_kwh.items():
# MAGIC         mlflow.log_metric(f"avg_kwh_{appliance}", round(kwh, 3))
# MAGIC         mlflow.log_metric(f"pct_{appliance}",     round(kwh / total * 100, 1))
# MAGIC
# MAGIC     # Log the fitted pipeline as a Spark ML model artifact
# MAGIC     mlflow.spark.log_model(model, "nilm_pipeline")
# MAGIC ```
# MAGIC
# MAGIC This notebook keeps MLflow commented out to stay self-contained on
# MAGIC Databricks Free edition — no experiment server configuration required.
# MAGIC Uncomment the block above when running on a workspace with MLflow enabled.
