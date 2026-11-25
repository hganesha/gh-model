from orders.clients.inventory import reserve
from orders.commands.place_order import place_order
from orders.events import publish_placed
from orders.repositories.order_repository import OrderRepository


class OrderService:
    def __init__(self) -> None:
        self.repository = OrderRepository()

    def get(self, order_id: int):
        return self.repository.get(order_id)

    def place(self, order: dict) -> int:
        reserve(order["sku"])
        order_id = place_order(order["customer_id"], order["total"])
        publish_placed(order_id)
        return order_id
