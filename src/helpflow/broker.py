import json
from collections.abc import Mapping
from typing import Any

import pika

from helpflow.config import get_settings

EXCHANGE = "helpflow.events"
NOTIFICATION_QUEUE = "helpflow.notifications"


def connect() -> pika.BlockingConnection:
    parameters = pika.URLParameters(get_settings().rabbitmq_url)
    parameters.connection_attempts = 3
    parameters.retry_delay = 1
    parameters.socket_timeout = 5
    parameters.blocked_connection_timeout = 5
    return pika.BlockingConnection(parameters)


def declare_topology(channel: pika.channel.Channel) -> None:
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(queue=NOTIFICATION_QUEUE, durable=True)
    channel.queue_bind(queue=NOTIFICATION_QUEUE, exchange=EXCHANGE, routing_key="ticket.*")


def publish_event(channel: pika.channel.Channel, event_type: str, body: Mapping[str, Any]) -> None:
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=event_type,
        body=json.dumps(body, ensure_ascii=False).encode(),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=str(body["event_id"]),
            type=event_type,
        ),
        mandatory=True,
    )


def check_connection() -> None:
    connection = connect()
    connection.close()
