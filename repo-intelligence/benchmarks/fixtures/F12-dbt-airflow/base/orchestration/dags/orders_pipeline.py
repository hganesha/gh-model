from datetime import datetime

from airflow import DAG, Dataset
from airflow.operators.bash import BashOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

DAILY_REPORT = Dataset("snowflake://reporting/daily_orders")

with DAG("orders_pipeline", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    extract = SparkSubmitOperator(task_id="extract", application="orchestration/jobs/extract_orders.py", conn_id="spark")
    build = BashOperator(task_id="dbt_build", bash_command="dbt build --project-dir /opt/analytics")
    publish = SQLExecuteQueryOperator(
        task_id="publish",
        conn_id="snowflake",
        sql="INSERT INTO reporting.daily_orders SELECT order_date, sum(amount) FROM analytics.fct_orders GROUP BY 1",
        outlets=[DAILY_REPORT],
    )
    extract >> build >> publish
