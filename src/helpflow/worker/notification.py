import json
import logging
import random
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pika
from sqlalchemy import and_, or_, select, text, update

from helpflow.broker import INVALID_QUEUE, NOTIFICATION_QUEUE, connect, declare_topology
from helpflow.config import get_settings
from helpflow.db import SessionLocal
from helpflow.models import DeliveryStatus, NotificationDelivery, OutboxEvent, User

settings = get_settings()
logger = logging.getLogger("helpflow.notification")
MAX_ATTEMPTS = 6
LEASE_SECONDS = 60


class DeliveryError(Exception):
    def __init__(self, code: str, *, permanent: bool = False, retry_after: int = 0) -> None:
        super().__init__(code)
        self.permanent = permanent
        self.retry_after = retry_after


def _recipients(event_type: str, payload: dict[str, Any]) -> list[str]:
    client = payload.get("client_chat_id")
    operator = payload.get("operator_chat_id")
    if event_type == "ticket.created" or payload.get("internal"):
        candidates = [operator]
    elif event_type in {"ticket.assigned", "ticket.status_changed"}:
        candidates = [client]
    else:
        candidates = [client, operator]
    return list(dict.fromkeys(str(value) for value in candidates if value))


def _message(event_type: str, payload: dict[str, Any]) -> str:
    # Подробности доступны только после авторизации в API, в Telegram их не копируем.
    return f"HelpFlow: обновлена заявка #{payload.get('ticket_number')}. Событие: {event_type}."


def _send_telegram(chat_id: str, message: str) -> None:
    token = settings.telegram_bot_token
    if token is None or not token.get_secret_value():
        raise DeliveryError("bot_not_configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read(65536))
        except (ValueError, OSError):
            result = {"ok": False, "error_code": exc.code}
    except (OSError, ValueError) as exc:
        raise DeliveryError("transport_error") from exc
    if not isinstance(result, dict):
        raise DeliveryError("invalid_response")
    if result.get("ok") is not True:
        code = result.get("error_code", 500)
        parameters = result.get("parameters") or {}
        retry_after = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        if not isinstance(retry_after, int):
            retry_after = 0
        raise DeliveryError(
            f"telegram_{code}",
            permanent=code in {400, 403, 404},
            retry_after=max(0, min(retry_after, 86400)),
        )


def process_event(body: dict[str, Any]) -> None:
    event_id = UUID(str(body["event_id"]))
    with SessionLocal.begin() as db:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 202))"),
            {"key": str(event_id)},
        )
        if db.scalar(
            select(NotificationDelivery.id)
            .where(NotificationDelivery.event_id == event_id)
            .limit(1)
        ):
            return
        event = db.get(OutboxEvent, event_id)
        if event is None or body.get("event_type") != event.event_type:
            raise ValueError("Событие отсутствует в outbox или имеет другой тип")
        recipients = _recipients(event.event_type, event.payload)
        for recipient in recipients or [""]:
            db.add(
                NotificationDelivery(
                    event_id=event_id,
                    event_type=event.event_type,
                    recipient=recipient,
                    payload=event.payload,
                    status="pending" if recipient else "skipped",
                    error=None if recipient else "recipient_not_configured",
                )
            )


def claim_delivery() -> tuple[UUID, UUID, str, str, dict[str, Any], int] | None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(NotificationDelivery)
            .where(
                or_(
                    and_(
                        NotificationDelivery.status.in_(["pending", "retry"]),
                        NotificationDelivery.next_attempt_at <= now,
                    ),
                    and_(
                        NotificationDelivery.status == "sending",
                        NotificationDelivery.lease_until <= now,
                    ),
                )
            )
            .order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = DeliveryStatus.DEAD, "attempts_exhausted"
            job.lease_until, job.lease_token = None, None
            return None
        job.status = DeliveryStatus.SENDING
        job.attempts += 1
        job.lease_token = uuid4()
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return (
            job.id,
            job.lease_token,
            job.recipient or "",
            job.event_type,
            job.payload,
            job.attempts,
        )


def finish_delivery(
    job_id: UUID, token: UUID, *, status: str, error: str | None, delay: float = 0
) -> bool:
    with SessionLocal.begin() as db:
        result = db.scalar(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == job_id,
                NotificationDelivery.lease_token == token,
                NotificationDelivery.status == "sending",
            )
            .values(
                status=status,
                error=error,
                lease_token=None,
                lease_until=None,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
            )
            .returning(NotificationDelivery.id)
        )
        return result is not None


def deliver_one() -> bool:
    claimed = claim_delivery()
    if claimed is None:
        return False
    job_id, token, recipient, event_type, payload, attempt = claimed
    state, error, delay = "sent", None, 0.0
    try:
        # Старая привязка не даёт права отправлять уведомления после смены чата.
        if recipient == payload.get("client_chat_id"):
            with SessionLocal() as db:
                user = db.get(User, UUID(str(payload["client_id"])))
                if (
                    user is None
                    or not user.is_active
                    or not user.telegram_verified_at
                    or user.telegram_chat_id != recipient
                ):
                    raise DeliveryError("recipient_changed", permanent=True)
        _send_telegram(recipient, _message(event_type, payload))
    except DeliveryError as exc:
        error = str(exc)
        state = "dead" if exc.permanent or attempt >= MAX_ATTEMPTS else "retry"
        delay = max(exc.retry_after, min(300, 2**attempt) + random.uniform(0, 1))
    except Exception as exc:
        error = type(exc).__name__
        state = "dead" if attempt >= MAX_ATTEMPTS else "retry"
        delay = min(300, 2**attempt)
    accepted = finish_delivery(job_id, token, status=state, error=error, delay=delay)
    logger.info("delivery id=%s attempt=%s status=%s accepted=%s", job_id, attempt, state, accepted)
    return True


def consume_one(channel: pika.channel.Channel) -> bool:
    method, _properties, body = channel.basic_get(NOTIFICATION_QUEUE, auto_ack=False)
    if method is not None:
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError("Ожидается объект")
            process_event(event)
        except (ValueError, KeyError, TypeError):
            # Сначала ждём записи в карантин, чтобы не потерять сообщение.
            channel.basic_publish(
                exchange="",
                routing_key=INVALID_QUEUE,
                body=body,
                properties=pika.BasicProperties(delivery_mode=2),
                mandatory=True,
            )
        channel.basic_ack(method.delivery_tag)
    return method is not None


def run() -> None:
    logging.basicConfig(level=settings.log_level.upper())
    logging.getLogger("pika").setLevel(logging.WARNING)
    while True:
        connection = None
        try:
            deliver_one()
            connection = connect()
            channel = connection.channel()
            declare_topology(channel)
            channel.confirm_delivery()
            while connection.is_open:
                consumed = consume_one(channel)
                worked = deliver_one()
                connection.process_data_events(time_limit=0 if worked or consumed else 1)
        except Exception as exc:
            logger.warning("worker_retry kind=%s", type(exc).__name__)
            time.sleep(3)
        finally:
            if connection is not None and connection.is_open:
                connection.close()


if __name__ == "__main__":
    run()
