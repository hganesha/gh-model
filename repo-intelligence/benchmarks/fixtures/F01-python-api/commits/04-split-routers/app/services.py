from .db import Session
from .domain.models import Customer


def load_customer(customer_id: int):
    with Session() as session:
        return session.get(Customer, customer_id)


class OrderService:
    def total(self, customer) -> int:
        return self._sum(customer)

    def _sum(self, customer) -> int:
        return 0
