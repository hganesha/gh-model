import json

from confluent_kafka import Producer

from .topics import ORDER_CREATED, ORDER_CANCELED

producer = Producer({"bootstrap.servers": "kafka:9092"})


def publish_created(order: dict) -> None:
    producer.produce(ORDER_CREATED, json.dumps(order).encode())


def publish_cancelled(order_id: int) -> None:
    producer.produce(ORDER_CANCELED, json.dumps({"id": order_id}).encode())
