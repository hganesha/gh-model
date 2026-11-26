from fastapi import FastAPI

from .db import Session
from .models import Order

app = FastAPI()


@app.get("/api/orders")
def list_orders():
    with Session() as session:
        return [{"id": o.id, "status": o.status} for o in session.query(Order).all()]


@app.post("/api/orders")
def place_order(customer_id: int, total: float):
    with Session() as session:
        order = Order(customer_id=customer_id, status="new", total=total)
        session.add(order)
        session.commit()
        return {"id": order.id}


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    with Session() as session:
        order = session.get(Order, order_id)
        return {"id": order.id, "status": order.status, "total": order.total}


@app.get("/api/customers")
def list_customers():
    # The front end already calls this path; until this commit no member served it.
    return []
