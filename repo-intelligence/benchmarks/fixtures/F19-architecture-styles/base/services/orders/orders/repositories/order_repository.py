from orders.repositories.models import Order, SessionLocal


class OrderRepository:
    def get(self, order_id: int):
        with SessionLocal() as session:
            return session.get(Order, order_id)
