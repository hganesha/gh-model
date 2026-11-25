import httpx
import pybreaker
from tenacity import retry, stop_after_attempt, wait_exponential

from payments.ports.gateway import PaymentGateway

# Stop calling the card network after five failures in a row; try again after 30 seconds.
breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)


class CardGateway(PaymentGateway):
    @breaker
    @retry(wait=wait_exponential(multiplier=0.5), stop=stop_after_attempt(4))
    def charge(self, customer_id: int, amount: int) -> bool:
        return httpx.post("https://cards.example/charge", json={"customer": customer_id, "amount": amount}, timeout=2.0).is_success
