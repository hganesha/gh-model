import json

from confluent_kafka import Consumer, Producer

from orders.commands.place_order import cancel_order

producer = Producer({"bootstrap.servers": "kafka:9092"})
consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "orders-saga"})
consumer.subscribe(["payments.failed"])


def publish_placed(order_id: int) -> None:
    producer.produce("orders.placed", json.dumps({"order_id": order_id}).encode())


def compensate() -> None:
    """Undo a placed order when its payment fails."""
    while True:
        message = consumer.poll(1.0)
        if message is not None:
            cancel_order(json.loads(message.value())["order_id"])
