import json

from confluent_kafka import Consumer, Producer

from payments.adapters.card_gateway import CardGateway
from payments.core.charge import settle

consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "payments"})
consumer.subscribe(["orders.placed"])
producer = Producer({"bootstrap.servers": "kafka:9092"})


def run() -> None:
    gateway = CardGateway()
    while True:
        message = consumer.poll(1.0)
        if message is None:
            continue
        order = json.loads(message.value())
        if not settle(gateway, order):
            producer.produce("payments.failed", json.dumps({"order_id": order["order_id"]}).encode())
