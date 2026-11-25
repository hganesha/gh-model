from sqlalchemy import select

from orders.repositories.models import OrderSummary, SessionLocal


def order_summary(customer_id: int):
    with SessionLocal() as session:
        return session.scalars(select(OrderSummary).where(OrderSummary.customer_id == customer_id)).all()
