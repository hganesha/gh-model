from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Customer, Order


def orders_for(session: Session, customer_id: int) -> list[Order]:
    return list(session.scalars(select(Order).where(Order.customer_id == customer_id)))


def add_customer(session: Session, email: str) -> Customer:
    customer = Customer(email=email)
    session.add(customer)
    return customer


def count_rows(session: Session, table_name: str) -> int:
    # Dynamic SQL: the table is chosen at runtime and cannot be resolved statically.
    return session.execute(text(f"SELECT count(*) FROM {table_name}")).scalar_one()
