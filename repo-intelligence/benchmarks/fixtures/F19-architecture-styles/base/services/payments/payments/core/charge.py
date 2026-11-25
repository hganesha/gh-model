from payments.ports.gateway import PaymentGateway


def settle(gateway: PaymentGateway, order: dict) -> bool:
    if order["total"] <= 0:
        return False
    return gateway.charge(order["customer_id"], order["total"])
