import json

from confluent_kafka import Producer

from .topics import ORDER_CREATED

producer = Producer({"bootstrap.servers": "kafka:9092"})


def publish_created(order: dict) -> None:
    producer.produce(ORDER_CREATED, json.dumps(order).encode())
