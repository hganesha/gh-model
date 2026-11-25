import httpx
from tenacity import retry


@retry
def reserve(sku: str) -> dict:
    return httpx.post("http://inventory/reserve", json={"sku": sku}).json()
