TOOLS = [
    {"name": "lookup_order", "description": "Find an order by id", "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}}}},
    {"name": "refund_order", "description": "Refund an order", "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}}}},
]


def lookup_order(order_id: int) -> dict:
    return {"id": order_id, "status": "shipped"}
