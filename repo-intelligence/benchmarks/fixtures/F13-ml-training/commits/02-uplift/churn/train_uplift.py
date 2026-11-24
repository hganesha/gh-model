import mlflow
import mlflow.lightgbm
import pandas as pd
from lightgbm import LGBMClassifier


def main() -> None:
    frame = pd.read_parquet("data/features.parquet")
    model = LGBMClassifier().fit(frame.drop(columns="treated"), frame["treated"])
    mlflow.lightgbm.log_model(model, "model", registered_model_name="uplift_model")
