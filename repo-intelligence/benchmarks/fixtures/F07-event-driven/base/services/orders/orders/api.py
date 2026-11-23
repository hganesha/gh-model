from fastapi import FastAPI

from .publisher import publish_created

app = FastAPI()


@app.post("/orders")
def create(order: dict):
    publish_created(order)
    return {"accepted": True}
