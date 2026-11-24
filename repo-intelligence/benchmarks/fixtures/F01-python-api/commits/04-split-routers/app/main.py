from fastapi import FastAPI

from .admin import admin
from .db import Session
from .domain.models import Customer
from .routers import orders

app = FastAPI()
app.include_router(orders.router, prefix="/api/v1")
app.mount("/admin", admin)


@app.get("/customers")
def list_customers():
    with Session() as session:
        return [{"id": c.id, "email": c.email} for c in session.query(Customer).all()]


@app.post("/customers")
def create_customer(email: str):
    with Session() as session:
        customer = Customer(email=email)
        session.add(customer)
        session.commit()
        return {"id": customer.id}
