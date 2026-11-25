from orders.repositories.models import Order, SessionLocal


def place_order(customer_id: int, total: int) -> int:
    with SessionLocal() as session:
        order = Order(customer_id=customer_id, total=total)
        session.add(order)
        session.commit()
        return order.id


def cancel_order(order_id: int) -> None:
    with SessionLocal() as session:
        order = Order(id=order_id, status="cancelled")
        session.merge(order)
        session.add(order)
        session.commit()
