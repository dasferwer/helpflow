from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update

from helpflow.db import SessionLocal
from helpflow.models import (
    DeliveryReplay,
    NotificationDelivery,
    OutboxEvent,
    TelegramLink,
    Ticket,
    TicketHistory,
)
from helpflow.worker import notification as worker

HOOK = "/api/v1/integrations/telegram/webhook"
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}


def parallel(*operations):
    barrier = Barrier(len(operations))

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def webhook(client, update_id, text, chat_id=222222, chat_type="private"):
    return client.post(
        HOOK,
        headers=SECRET,
        json={
            "update_id": update_id,
            "message": {"text": text, "chat": {"id": chat_id, "type": chat_type}},
        },
    )


def test_link_requires_one_time_code_and_private_chat(client, register_client):
    owner = register_client()
    assert (
        client.put(
            "/api/v1/users/me/telegram", headers=owner["headers"], json={"chat_id": "222222"}
        ).status_code
        == 404
    )
    code = client.post("/api/v1/users/me/telegram/link-token", headers=owner["headers"]).json()[
        "token"
    ]
    assert not webhook(client, 1001, "/start " + code, chat_type="group").json()["accepted"]
    replies = parallel(*(lambda i=i: webhook(client, 1010 + i, "/start " + code) for i in range(4)))
    assert sum(r.json()["accepted"] for r in replies) == 1
    assert (
        client.get("/api/v1/users/me", headers=owner["headers"]).json()["telegram_chat_id"]
        == "222222"
    )
    assert not webhook(client, 1020, "/start " + code, chat_id=333333).json()["accepted"]
    newer = client.post("/api/v1/users/me/telegram/link-token", headers=owner["headers"]).json()[
        "token"
    ]
    with SessionLocal.begin() as db:
        db.execute(
            update(TelegramLink)
            .where(TelegramLink.user_id == UUID(owner["id"]))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert not webhook(client, 1021, "/start " + newer).json()["accepted"]


def test_webhook_replay_creates_one_ticket_and_rejects_invalid_text(client, register_client):
    owner = register_client()
    code = client.post("/api/v1/users/me/telegram/link-token", headers=owner["headers"]).json()[
        "token"
    ]
    assert webhook(client, 1100, "/start " + code, chat_id=444444).json()["accepted"]
    text = "/new Проверка повтора | Повторная доставка одного события Telegram"
    replies = parallel(*(lambda: webhook(client, 1101, text, chat_id=444444) for _ in range(6)))
    assert all(r.status_code == 200 for r in replies)
    assert all(r.json() == replies[0].json() for r in replies)
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.client_id == UUID(owner["id"]))
            )
            == 1
        )
    assert webhook(client, 1101, text + "!", chat_id=444444).status_code == 409
    bad = webhook(client, 1102, "/new a | b", chat_id=444444)
    assert bad.status_code == 200 and not bad.json()["accepted"]


def test_ticket_version_prevents_lost_transition(
    client, register_client, create_ticket, operator_headers
):
    owner = register_client()
    ticket = create_ticket(owner["headers"])
    staff = client.get("/api/v1/users/staff", headers=operator_headers).json()[0]
    path = f"/api/v1/tickets/{ticket['id']}"
    replies = parallel(
        lambda: client.post(
            path + "/assign",
            headers=operator_headers,
            json={"assignee_id": staff["id"], "version": 1},
        ),
        lambda: client.post(
            path + "/transition",
            headers=operator_headers,
            json={"status": "in_progress", "version": 1},
        ),
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(TicketHistory)
                .where(TicketHistory.ticket_id == UUID(ticket["id"]))
            )
            == 2
        )
    assert client.get(path, headers=owner["headers"]).json()["version"] == 2


@pytest.fixture
def event():
    with SessionLocal.begin() as db:
        db.execute(delete(DeliveryReplay))
        db.execute(delete(NotificationDelivery))
        record = OutboxEvent(
            event_type="ticket.comment_added",
            payload={
                "ticket_number": 99,
                "operator_chat_id": "10001",
                "client_chat_id": None,
            },
        )
        db.add(record)
        db.flush()
        event_id = record.id
    return {"event_id": str(event_id), "event_type": "ticket.comment_added"}


def test_consumer_deduplicates_and_retries_transient_failure(event, monkeypatch):
    parallel(*(lambda: worker.process_event(event) for _ in range(8)))
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
    sent = []

    def send(chat, message):
        sent.append(chat)
        if len(sent) == 1:
            raise worker.DeliveryError("telegram_429", retry_after=120)

    monkeypatch.setattr(worker, "_send_telegram", send)
    before = datetime.now(UTC)
    assert worker.deliver_one()
    with SessionLocal.begin() as db:
        job = db.scalar(select(NotificationDelivery))
        assert job.status == "retry" and job.attempts == 1
        assert job.next_attempt_at >= before + timedelta(seconds=120)
        job.next_attempt_at = before
    assert worker.deliver_one()
    assert not worker.deliver_one()
    worker.process_event(event)
    assert sent == ["10001", "10001"]
    with SessionLocal() as db:
        assert db.scalar(select(NotificationDelivery.status)) == "sent"


def test_lease_recovery_fences_stale_worker(event):
    worker.process_event(event)
    first = worker.claim_delivery()
    assert first is not None
    assert worker.claim_delivery() is None
    with SessionLocal.begin() as db:
        db.execute(
            update(NotificationDelivery).values(
                lease_until=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    second = worker.claim_delivery()
    assert second is not None and second[1] != first[1]
    assert not worker.finish_delivery(first[0], first[1], status="sent", error=None)
    assert worker.finish_delivery(second[0], second[1], status="sent", error=None)


def test_permanent_failure_and_exhausted_attempts_go_to_dead(event, monkeypatch):
    worker.process_event(event)

    def fail(*args):
        raise worker.DeliveryError("telegram_403", permanent=True)

    monkeypatch.setattr(worker, "_send_telegram", fail)
    assert worker.deliver_one()
    with SessionLocal.begin() as db:
        job = db.scalar(select(NotificationDelivery))
        assert job.status == "dead" and job.attempts == 1
        job.status, job.attempts = "retry", worker.MAX_ATTEMPTS
        job.next_attempt_at = datetime.now(UTC)
    assert not worker.deliver_one()
    with SessionLocal() as db:
        assert db.scalar(select(NotificationDelivery.status)) == "dead"


def test_internal_event_has_no_client_recipient_and_history(
    client, register_client, create_ticket, operator_headers
):
    owner = register_client()
    ticket = create_ticket(owner["headers"])
    path = f"/api/v1/tickets/{ticket['id']}"
    client.post(
        path + "/comments",
        headers=operator_headers,
        json={"body": "Внутреннее расследование", "is_internal": True},
    )
    details = client.get(path, headers=owner["headers"]).json()
    assert not details["comments"]
    assert all(not row["details"].get("internal") for row in details["history"])
    assert worker._recipients(
        "ticket.comment_added",
        {
            "internal": True,
            "client_chat_id": "123",
            "operator_chat_id": "456",
        },
    ) == ["456"]


def test_each_recipient_has_independent_state(event, monkeypatch):
    with SessionLocal.begin() as db:
        record = db.get(OutboxEvent, UUID(event["event_id"]))
        record.payload = {**record.payload, "client_chat_id": "20002"}
    worker.process_event(event)
    with SessionLocal.begin() as db:
        jobs = list(db.scalars(select(NotificationDelivery)).all())
        assert len(jobs) == 2
        for job in jobs:
            if job.recipient == "20002":
                job.status = "sent"
    calls = []
    monkeypatch.setattr(worker, "_send_telegram", lambda chat, message: calls.append(chat))
    assert worker.deliver_one()
    worker.process_event(event)
    assert not worker.deliver_one()
    assert calls == ["10001"]


def test_broker_delivery_and_invalid_message_quarantine(event):
    import json

    import pika

    from helpflow.broker import INVALID_QUEUE, NOTIFICATION_QUEUE, connect, declare_topology
    from helpflow.config import get_settings
    from helpflow.worker.publisher import publish_batch

    assert "rabbitmq-test" in get_settings().rabbitmq_url
    connection = connect()
    try:
        channel = connection.channel()
        declare_topology(channel)
        channel.confirm_delivery()
        channel.queue_purge(NOTIFICATION_QUEUE)
        channel.queue_purge(INVALID_QUEUE)
        assert publish_batch(channel) > 0
        target_received = False
        while True:
            method, _, body = channel.basic_get(NOTIFICATION_QUEUE, auto_ack=False)
            if method is None:
                break
            message = json.loads(body)
            if message["event_id"] == event["event_id"]:
                worker.process_event(message)
                worker.process_event(message)
                target_received = True
            channel.basic_ack(method.delivery_tag)
        assert target_received
        channel.basic_publish(
            exchange="",
            routing_key=NOTIFICATION_QUEUE,
            body=b"{broken",
            properties=pika.BasicProperties(delivery_mode=2),
            mandatory=True,
        )
        assert worker.consume_one(channel)
        method, _, body = channel.basic_get(INVALID_QUEUE, auto_ack=True)
        assert method is not None and body == b"{broken"
    finally:
        connection.close()


def test_admin_retry_is_audited_and_staff_cannot_use_it(event, client, operator_headers):
    from helpflow.config import get_settings
    from helpflow.models import DeliveryReplay, DeliveryStatus

    worker.process_event(event)
    with SessionLocal.begin() as db:
        job = db.scalar(select(NotificationDelivery))
        job.status = DeliveryStatus.DEAD
        job_id = job.id
    settings = get_settings()
    token = client.post(
        "/api/v1/auth/login",
        json={
            "email": str(settings.admin_email),
            "password": settings.admin_password.get_secret_value(),
        },
    ).json()["access_token"]
    path = f"/api/v1/notifications/{job_id}/retry"
    assert (
        client.post(
            path, headers=operator_headers, json={"reason": "Повтор после исправления"}
        ).status_code
        == 403
    )
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.post(path, headers=headers, json={"reason": "Повтор после исправления"}).status_code
        == 200
    )
    assert (
        client.post(path, headers=headers, json={"reason": "Повтор после исправления"}).status_code
        == 409
    )
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(DeliveryReplay)
                .where(DeliveryReplay.delivery_id == job_id)
            )
            == 1
        )


def test_telegram_error_classification(monkeypatch):
    import io
    import urllib.error

    from pydantic import SecretStr

    monkeypatch.setattr(worker.settings, "telegram_bot_token", SecretStr("test-token"))

    def limited(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid",
            429,
            "limit",
            {},
            io.BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":17}}'),
        )

    monkeypatch.setattr(worker.urllib.request, "urlopen", limited)
    with pytest.raises(worker.DeliveryError) as failure:
        worker._send_telegram("10001", "Проверка")
    assert not failure.value.permanent and failure.value.retry_after == 17
    assert "test-token" not in str(failure.value)


def test_outbox_failure_rolls_back_ticket(client, register_client, monkeypatch):
    owner = register_client()

    def fail(*args, **kwargs):
        raise RuntimeError("Искусственный сбой outbox")

    monkeypatch.setattr("helpflow.services.add_outbox_event", fail)
    with pytest.raises(RuntimeError, match="Искусственный сбой outbox"):
        client.post(
            "/api/v1/tickets",
            headers=owner["headers"],
            json={
                "subject": "Проверка атомарности",
                "description": "Создание должно полностью откатиться",
            },
        )
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.client_id == UUID(owner["id"]))
            )
            == 0
        )


def test_obsolete_recipient_is_not_contacted(event, register_client, monkeypatch):
    owner = register_client()
    with SessionLocal.begin() as db:
        record = db.get(OutboxEvent, UUID(event["event_id"]))
        record.event_type = "ticket.status_changed"
        record.payload = {"client_id": owner["id"], "client_chat_id": "123456", "ticket_number": 42}
    event["event_type"] = "ticket.status_changed"
    worker.process_event(event)
    calls = []
    monkeypatch.setattr(worker, "_send_telegram", lambda chat, message: calls.append(chat))
    assert worker.deliver_one()
    assert calls == []
    with SessionLocal() as db:
        job = db.scalar(select(NotificationDelivery))
        assert job.status == "dead" and job.error == "recipient_changed"


def test_chat_cannot_be_taken_by_another_account(client, register_client):
    first, second = register_client(), register_client()
    codes = [
        client.post("/api/v1/users/me/telegram/link-token", headers=user["headers"]).json()["token"]
        for user in (first, second)
    ]
    assert webhook(client, 1200, "/start " + codes[0], chat_id=555555).json()["accepted"]
    denied = webhook(client, 1201, "/start " + codes[1], chat_id=555555)
    assert denied.status_code == 200 and not denied.json()["accepted"]
    assert webhook(client, 1201, "/start " + codes[1], chat_id=555555).json() == denied.json()
    assert (
        client.get("/api/v1/users/me", headers=second["headers"]).json()["telegram_chat_id"] is None
    )
