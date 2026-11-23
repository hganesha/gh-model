from confluent_kafka import Consumer

consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "notifier"})
consumer.subscribe(["orders.created", "orders.cancelled"])


def run() -> None:
    while True:
        message = consumer.poll(1.0)
        if message is not None:
            print("notify", message.value())
