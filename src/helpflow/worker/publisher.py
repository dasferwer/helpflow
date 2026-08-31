import logging
import time
from datetime import UTC, datetime

import pika
from sqlalchemy import select

from helpflow.broker import connect, declare_topology, publish_event
from helpflow.config import get_settings
from helpflow.db import SessionLocal
from helpflow.models import OutboxEvent

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("helpflow.publisher")
logging.getLogger("pika").setLevel(logging.WARNING)


def publish_batch(channel: pika.channel.Channel) -> int:
    with SessionLocal.begin() as db:
        events = list(
            db.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .with_for_update(skip_locked=True)
                .limit(50)
            ).all()
        )
        for event in events:
            body = {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "occurred_at": event.created_at.isoformat(),
                "payload": event.payload,
            }
            publish_event(channel, event.event_type, body)
            event.published_at = datetime.now(UTC)
            event.attempts += 1
            event.last_error = None
        return len(events)


def run() -> None:
    logger.info("Outbox publisher started")
    while True:
        connection: pika.BlockingConnection | None = None
        try:
            connection = connect()
            channel = connection.channel()
            declare_topology(channel)
            channel.confirm_delivery()
            while connection.is_open:
                published = publish_batch(channel)
                if published:
                    time.sleep(0.2)
                else:
                    connection.process_data_events(time_limit=1)
        except Exception:
            logger.exception("Outbox publish failed")
            time.sleep(3)
        finally:
            if connection is not None and connection.is_open:
                connection.close()


if __name__ == "__main__":
    run()
