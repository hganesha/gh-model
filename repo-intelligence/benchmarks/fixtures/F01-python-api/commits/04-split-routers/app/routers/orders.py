from fastapi import APIRouter

from ..services import OrderService, load_customer

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{customer_id}/total")
def customer_total(customer_id: int):
    customer = load_customer(customer_id)
    service = OrderService()
    return {"total": service.total(customer)}
