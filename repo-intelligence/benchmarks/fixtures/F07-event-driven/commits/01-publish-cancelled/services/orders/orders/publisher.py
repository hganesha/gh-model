import json

from confluent_kafka import Producer

from .topics import ORDER_CREATED, ORDER_CANCELLED

producer = Producer({"bootstrap.servers": "kafka:9092"})


def publish_created(order: dict) -> None:
    producer.produce(ORDER_CREATED, json.dumps(order).encode())


def publish_cancelled(order_id: int) -> None:
    producer.produce(ORDER_CANCELLED, json.dumps({"id": order_id}).encode())
