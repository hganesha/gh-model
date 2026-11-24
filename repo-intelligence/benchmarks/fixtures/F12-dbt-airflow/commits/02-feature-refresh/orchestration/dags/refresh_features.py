from datetime import datetime

from airflow import Dataset
from airflow.decorators import dag, task

DAILY_REPORT = Dataset("snowflake://reporting/daily_orders")


@dag(schedule=[DAILY_REPORT], start_date=datetime(2024, 1, 1), catchup=False)
def refresh_features():
    @task
    def load():
        import pandas as pd
        return pd.read_parquet("s3://shop-features/orders.parquet")

    @task
    def score(frame):
        frame.to_parquet("s3://shop-features/scored.parquet")

    score(load())


refresh_features()
