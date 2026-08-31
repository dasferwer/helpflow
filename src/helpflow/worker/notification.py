import json
import logging
import urllib.request
from typing import Any
from uuid import UUID

import pika
from sqlalchemy import select

from helpflow.broker import NOTIFICATION_QUEUE, connect, declare_topology
from helpflow.config import get_settings
from helpflow.db import SessionLocal
from helpflow.models import DeliveryStatus, NotificationDelivery

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("helpflow.notification")
logging.getLogger("pika").setLevel(logging.WARNING)


def _recipients(event_type: str, payload: dict[str, Any]) -> list[str]:
    client = payload.get("client_chat_id")
    operator = payload.get("operator_chat_id")
    if event_type == "ticket.created":
        candidates = [operator]
    elif event_type in {"ticket.assigned", "ticket.status_changed"}:
        candidates = [client]
    else:
        candidates = [client, operator]
    return list(dict.fromkeys(str(value) for value in candidates if value))


def _message(event_type: str, payload: dict[str, Any]) -> str:
    return (
        f"HelpFlow #{payload.get('ticket_number')}: {payload.get('subject')}\n"
        f"Event: {event_type}\nStatus: {payload.get('status')}"
    )


def _send_telegram(chat_id: str, message: str) -> None:
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise RuntimeError("Telegram bot token is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        if response.status >= 300:
            raise RuntimeError(f"Telegram returned HTTP {response.status}")


def process_event(body: dict[str, Any]) -> None:
    event_id = UUID(str(body["event_id"]))
    event_type = str(body["event_type"])
    payload = dict(body["payload"])
    with SessionLocal.begin() as db:
        if db.scalar(
            select(NotificationDelivery.id).where(NotificationDelivery.event_id == event_id)
        ):
            return
        recipients = _recipients(event_type, payload)
        delivery_status = DeliveryStatus.SKIPPED
        error: str | None = None
        if recipients and settings.telegram_bot_token:
            try:
                message = _message(event_type, payload)
                for recipient in recipients:
                    _send_telegram(recipient, message)
                delivery_status = DeliveryStatus.SENT
            except Exception as exc:
                delivery_status = DeliveryStatus.FAILED
                error = str(exc)[:1000]
        else:
            error = "Telegram token or recipient is not configured"
        db.add(
            NotificationDelivery(
                event_id=event_id,
                event_type=event_type,
                recipient=",".join(recipients) or None,
                status=delivery_status,
                error=error,
            )
        )


def run() -> None:
    connection = connect()
    channel = connection.channel()
    declare_topology(channel)
    channel.basic_qos(prefetch_count=20)

    def callback(
        callback_channel: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.BasicProperties,
        body: bytes,
    ) -> None:
        del properties
        try:
            process_event(json.loads(body))
        except Exception:
            logger.exception("Notification event failed")
            callback_channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        callback_channel.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=NOTIFICATION_QUEUE, on_message_callback=callback)
    logger.info("Notification worker started")
    channel.start_consuming()


if __name__ == "__main__":
    run()
