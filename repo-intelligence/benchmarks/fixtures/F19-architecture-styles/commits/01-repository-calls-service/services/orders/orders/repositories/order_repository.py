from orders.repositories.models import Order, SessionLocal
from orders.services.order_service import OrderService


class OrderRepository:
    def get(self, order_id: int):
        with SessionLocal() as session:
            return session.get(Order, order_id)

    def priced(self, order_id: int):
        return OrderService().get(order_id)
