"""
dags/load_sample.py

Airflow DAG: smart_meter_nilm_pipeline

Orchestrates the four ingestion/feature/disaggregation/export scripts in
sequence. Designed for local and portfolio use — it runs the existing
scripts rather than reimplementing their logic.

To trigger manually:
    airflow dags trigger smart_meter_nilm_pipeline

# Airflow is not listed in requirements.txt because installation is
# environment-specific. Install separately:
#   pip install apache-airflow
"""

from datetime import datetime

from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

PROJECT_ROOT = "{{ var.value.get('smart_meter_project_root', '.') }}"

with DAG(
    dag_id="smart_meter_nilm_pipeline",
    description="Daily ingestion and feature pipeline for smart-meter-nilm",
    schedule="@daily",
    start_date=datetime(2025, 5, 1),
    catchup=False,
) as dag:

    ingest = BashOperator(
        task_id="ingest",
        bash_command=f"cd {PROJECT_ROOT} && python scripts/ingest.py",
    )

    features = BashOperator(
        task_id="features",
        bash_command=f"cd {PROJECT_ROOT} && python scripts/features.py",
    )

    disaggregate = BashOperator(
        task_id="disaggregate",
        bash_command=f"cd {PROJECT_ROOT} && python scripts/disaggregate.py",
    )

    export = BashOperator(
        task_id="export",
        bash_command=f"cd {PROJECT_ROOT} && python scripts/export.py",
    )

    ingest >> features >> disaggregate >> export
