import pandas as pd

RAW = "data/raw.csv"


def build() -> pd.DataFrame:
    events = pd.read_csv(RAW)
    features = events.groupby("customer_id").agg(sessions=("session_id", "nunique"), spend=("amount", "sum"))
    features.to_parquet("data/features.parquet")
    return features


if __name__ == "__main__":
    build()
