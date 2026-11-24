import mlflow
import mlflow.xgboost
import pandas as pd
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier


def main(max_depth: int = 6) -> None:
    frame = pd.read_parquet("data/features.parquet")
    x_train, x_test, y_train, y_test = train_test_split(frame.drop(columns="churned"), frame["churned"])
    with mlflow.start_run():
        model = XGBClassifier(max_depth=max_depth)
        model.fit(x_train, y_train)
        mlflow.log_metric("accuracy", model.score(x_test, y_test))
        mlflow.xgboost.log_model(model, "model", registered_model_name="churn_classifier")


if __name__ == "__main__":
    main()
