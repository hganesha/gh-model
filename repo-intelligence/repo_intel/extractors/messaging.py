"""Cloud message brokers in Python and TypeScript/JavaScript: Amazon SQS and SNS,
Google Pub/Sub, Azure Event Hubs and Service Bus, and RabbitMQ (pika, amqplib).
Kafka is read by `python.source` and `typescript.source`.

Each send or receive becomes a "topic_use" observation, which the linker turns into
a PUBLISHES_TO or SUBSCRIBES_TO edge from the package to an EventTopic named by the
queue or topic. Names are taken from string literals and same-file constants; a
queue URL or ARN is reduced to the name it ends in. A name read from the
environment or built at run time is recorded as dynamic: the linker counts it as
unresolved and reports it, it never guesses.

Where the name is given to a factory (a Service Bus sender, an Event Hubs client,
a Pub/Sub topic path), the factory call is the use: creating a sender is how code
publishes there.
"""

from __future__ import annotations

import ast
import re

from repo_intel.extractors.base import Extraction, span
from repo_intel.extractors.textscan import Doc, decode, queue_name, unquote, without_comments

# ------------------------------------------------------------------ Python

_PY_MODULES = {"boto3": None, "aioboto3": None, "google.cloud.pubsub": "pubsub", "google.cloud.pubsub_v1": "pubsub", "azure.eventhub": "eventhubs", "azure.servicebus": "servicebus", "pika": "rabbitmq", "aio_pika": "rabbitmq"}
# method -> (role, keyword holding the name, positional index or None, transport)
_PY_CALLS: dict[str, tuple[str, tuple[str, ...], int | None, str]] = {
    "send_message": ("publish", ("QueueUrl",), None, "sqs"),
    "send_message_batch": ("publish", ("QueueUrl",), None, "sqs"),
    "receive_message": ("subscribe", ("QueueUrl",), None, "sqs"),
    "publish": ("publish", ("TopicArn", "TargetArn", "topic"), 0, "sns|pubsub"),
    "get_queue_by_name": ("use", ("QueueName",), None, "sqs"),
    "get_queue_sender": ("publish", ("queue_name",), 0, "servicebus"),
    "get_queue_receiver": ("subscribe", ("queue_name",), 0, "servicebus"),
    "get_topic_sender": ("publish", ("topic_name",), 0, "servicebus"),
    "get_subscription_receiver": ("subscribe", ("topic_name",), 0, "servicebus"),
    "subscribe": ("subscribe", ("subscription",), 0, "pubsub"),
    "basic_publish": ("publish", ("routing_key",), None, "rabbitmq"),
    "basic_consume": ("subscribe", ("queue",), None, "rabbitmq"),
}
_PY_PATHS = {"topic_path": 1, "subscription_path": 1}  # publisher.topic_path(project, "orders")
_PY_EVENTHUB = {"EventHubProducerClient": "publish", "EventHubConsumerClient": "subscribe"}


def _kw(call: ast.Call, *names: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg in names), None)


class _PyScan:
    def __init__(self, tree: ast.Module, d: Doc, starts: list[int], transports: set[str]):
        self.d, self.starts, self.transports = d, starts, transports
        self.consts: dict[str, str] = {}
        self.paths: dict[str, str] = {}  # variable -> topic/subscription name from a *_path() call
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                self.consts[node.targets[0].id] = node.value.value
        self.tree = tree

    def at(self, n: ast.AST):
        return span(self.d.data, self.starts[n.lineno - 1] + n.col_offset, self.starts[(n.end_lineno or n.lineno) - 1] + (n.end_col_offset or n.col_offset))

    def value(self, node: ast.expr | None) -> tuple[str | None, str | None]:
        """(queue name, constant name) of an argument."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return queue_name(node.value), None
        if isinstance(node, ast.Name):
            if node.id in self.paths:
                return self.paths[node.id], None
            if node.id in self.consts:
                return queue_name(self.consts[node.id]), None
            return None, node.id
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _PY_PATHS:
            args = node.args
            idx = _PY_PATHS[node.func.attr]
            return self.value(args[idx] if len(args) > idx else None)[0], None
        return None, None

    def transport(self, options: str) -> str | None:
        for t in options.split("|"):
            if t in self.transports or (t in ("sqs", "sns") and "aws" in self.transports):
                return t
        return None

    def run(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call):
                f = node.value.func
                if isinstance(f, ast.Attribute) and f.attr in _PY_PATHS:
                    name, _ = self.value(node.value)
                    if name:
                        self.paths[node.targets[0].id] = name
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            attr = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            owner = f.value if isinstance(f, ast.Attribute) else None
            # EventHubProducerClient(...) / .from_connection_string(..., eventhub_name="x")
            ctor = attr if attr in _PY_EVENTHUB else (owner.id if isinstance(owner, ast.Name) and owner.id in _PY_EVENTHUB and attr == "from_connection_string" else None)
            if ctor and "eventhubs" in self.transports:
                name, const = self.value(_kw(node, "eventhub_name"))
                self.d.topic(self.at(node), _PY_EVENTHUB[ctor], "eventhubs", name, const)
                continue
            spec = _PY_CALLS.get(attr)
            if spec is None or owner is None:
                continue
            role, keywords, position, options = spec
            if attr == "publish":  # SNS names a TopicArn; Pub/Sub passes the topic path first
                options = "sns" if _kw(node, "TopicArn", "TargetArn") is not None else "pubsub"
            transport = self.transport(options)
            if transport is None:
                continue
            target = _kw(node, *keywords)
            if target is None and position is not None and len(node.args) > position:
                target = node.args[position]
            if target is None:
                continue
            if role == "use":  # resource.get_queue_by_name(QueueName=...): a handle, not a send
                continue
            name, const = self.value(target)
            self.d.topic(self.at(node), role, transport, name, const)


def _python(path: str, data: bytes, blob: str, text: str) -> Doc | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    transports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):  # `from google.cloud import pubsub_v1` imports google.cloud.pubsub_v1
            names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names if node.module]
        else:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
        for module in names:
            for prefix, transport in _PY_MODULES.items():
                if module == prefix or module.startswith(prefix + "."):
                    transports.add(transport or "aws")
    if not transports:
        return None
    if "aws" in transports:
        transports |= {"sqs", "sns"}
    starts = [0]
    for line in data.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    d = Doc(path, data, blob, text)
    _PyScan(tree, d, starts, transports).run()
    return d


# --------------------------------------------------------- TypeScript/JavaScript

_TS_PACKAGES = {"@aws-sdk/client-sqs": "sqs", "@aws-sdk/client-sns": "sns", "aws-sdk": "aws", "@google-cloud/pubsub": "pubsub",
                "@azure/service-bus": "servicebus", "@azure/event-hubs": "eventhubs", "amqplib": "rabbitmq", "amqp-connection-manager": "rabbitmq"}
_TS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]""")
_TS_CONST = re.compile(r"""\b(?:const|let|var)\s+(\w+)\s*(?::\s*\w+\s*)?=\s*(['"`][^'"`\n]*['"`])""")
_ARG = r"""(['"`][^'"`\n]*['"`]|[\w.$]+)"""
_TS_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(rf"new\s+(?:SendMessage|SendMessageBatch)Command\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_ARG}"), "publish", "sqs"),
    (re.compile(rf"new\s+ReceiveMessageCommand\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_ARG}"), "subscribe", "sqs"),
    (re.compile(rf"new\s+PublishCommand\(\s*\{{[^}}]*?(?:TopicArn|TargetArn)\s*:\s*{_ARG}"), "publish", "sns"),
    (re.compile(rf"\.sendMessage(?:Batch)?\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_ARG}"), "publish", "sqs"),
    (re.compile(rf"\.receiveMessage\(\s*\{{[^}}]*?QueueUrl\s*:\s*{_ARG}"), "subscribe", "sqs"),
    (re.compile(rf"\.topic\(\s*{_ARG}\s*\)\s*\.(?:publish|publishMessage|publishJSON)\b"), "publish", "pubsub"),
    (re.compile(rf"\.subscription\(\s*{_ARG}\s*\)"), "subscribe", "pubsub"),
    (re.compile(rf"\.createSender\(\s*{_ARG}"), "publish", "servicebus"),
    (re.compile(rf"\.createReceiver\(\s*{_ARG}"), "subscribe", "servicebus"),
    (re.compile(rf"new\s+EventHubProducerClient\(\s*[^,()]+,\s*{_ARG}"), "publish", "eventhubs"),
    (re.compile(rf"new\s+EventHubConsumerClient\(\s*[^,()]+,\s*[^,()]+,\s*{_ARG}"), "subscribe", "eventhubs"),
    (re.compile(rf"\.sendToQueue\(\s*{_ARG}"), "publish", "rabbitmq"),
    (re.compile(rf"\.consume\(\s*{_ARG}"), "subscribe", "rabbitmq"),
]


def _typescript(path: str, data: bytes, blob: str, raw: str) -> Doc | None:
    transports = {_TS_PACKAGES[m] for m in _TS_IMPORT.findall(raw) if m in _TS_PACKAGES}
    if not transports:
        return None
    if "aws" in transports:
        transports |= {"sqs", "sns"}
    text = without_comments(raw, "typescript")
    consts = {m.group(1): unquote(m.group(2)) for m in _TS_CONST.finditer(text)}
    d = Doc(path, data, blob, text)
    for rx, role, transport in _TS_PATTERNS:
        if transport not in transports:
            continue
        for m in rx.finditer(text):
            arg = m.group(1)
            literal = unquote(arg)
            name = queue_name(literal) if literal is not None else queue_name(consts[arg]) if consts.get(arg) else None
            const = arg if literal is None and name is None and re.fullmatch(r"[A-Za-z_$][\w$]*", arg) else None
            d.topic(d.at(m.start(), m.end()), role, transport, name, const)
    return d


class CloudMessaging:
    id = "messaging.cloud"
    version = "1.0.0"
    _SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs")
    _MARKERS = (b"boto3", b"pubsub", b"eventhub", b"servicebus", b"pika", b"@aws-sdk/client-s", b"aws-sdk", b"@google-cloud/pubsub", b"@azure/service-bus", b"@azure/event-hubs", b"amqp")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self._SUFFIXES) and not path.endswith(".d.ts")

    def extract(self, path, data, blob):
        if not any(m in data for m in self._MARKERS):
            return Extraction(parsed=False)
        text = decode(data)
        if text is None:
            return Extraction(parsed=False)
        d = _python(path, data, blob, text) if path.endswith(".py") else _typescript(path, data, blob, text)
        if d is None or not d.out:
            return Extraction(parsed=False)
        return Extraction(d.out)


EXTRACTORS = [CloudMessaging()]
