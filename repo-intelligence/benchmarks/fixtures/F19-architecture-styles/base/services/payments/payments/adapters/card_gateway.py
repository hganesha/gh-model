import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from payments.ports.gateway import PaymentGateway


class CardGateway(PaymentGateway):
    @retry(wait=wait_exponential(multiplier=0.5), stop=stop_after_attempt(4))
    def charge(self, customer_id: int, amount: int) -> bool:
        return httpx.post("https://cards.example/charge", json={"customer": customer_id, "amount": amount}).is_success
