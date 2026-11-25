import httpx
from tenacity import retry, stop_after_attempt, wait_random_exponential


@retry(wait=wait_random_exponential(max=10), stop=stop_after_attempt(5))
def reserve(sku: str) -> dict:
    return httpx.post("http://inventory/reserve", json={"sku": sku}).json()
