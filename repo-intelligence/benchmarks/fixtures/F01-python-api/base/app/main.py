from fastapi import FastAPI

from .db import Session
from .models import Customer

app = FastAPI()


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
