import os

from confluent_kafka import Consumer

consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "audit"})
# Topic names come from the environment at runtime: not statically resolvable.
consumer.subscribe(os.environ["AUDIT_TOPICS"].split(","))
