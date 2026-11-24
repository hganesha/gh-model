import mlflow.pyfunc
import pandas as pd

MODEL_URI = "models:/churn_classifier/Production"


def score() -> None:
    model = mlflow.pyfunc.load_model(MODEL_URI)
    frame = pd.read_parquet("data/features.parquet")
    frame.assign(churn=model.predict(frame)).to_parquet("data/scores.parquet")
