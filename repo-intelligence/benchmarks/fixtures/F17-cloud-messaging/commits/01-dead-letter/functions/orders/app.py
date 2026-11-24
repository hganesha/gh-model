import json
import os

import boto3

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/orders-queue"
NOTIFICATIONS = "arn:aws:sns:us-east-1:123456789012:order-notifications"

sqs = boto3.client("sqs")
sns = boto3.client("sns")


def handler(event, context):
    for record in event["Records"]:
        order = json.loads(record["body"])
        sns.publish(TopicArn=NOTIFICATIONS, Message=json.dumps(order))


def drain():
    # Read anything the trigger missed.
    return sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=10)


def dead_letter(order):
    sqs.send_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/orders-dlq", MessageBody=json.dumps(order))


def forward(order):
    sqs.send_message(QueueUrl=os.environ["FORWARD_QUEUE_URL"], MessageBody=json.dumps(order))
