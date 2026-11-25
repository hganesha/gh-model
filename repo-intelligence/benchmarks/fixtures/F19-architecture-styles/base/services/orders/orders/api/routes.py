from fastapi import FastAPI

from orders.services.order_service import OrderService

app = FastAPI()
service = OrderService()


@app.get("/orders/{order_id}")
def get_order(order_id: int):
    return service.get(order_id)


@app.post("/orders")
def place_order(order: dict):
    return {"id": service.place(order)}
