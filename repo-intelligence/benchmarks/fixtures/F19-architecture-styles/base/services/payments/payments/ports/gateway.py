from typing import Protocol


class PaymentGateway(Protocol):
    def charge(self, customer_id: int, amount: int) -> bool: ...


class Notifier(Protocol):
    async def notify(self, customer_id: int, text: str) -> None: ...
