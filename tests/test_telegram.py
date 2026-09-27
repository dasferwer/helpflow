from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from helpflow.db import SessionLocal
from helpflow.models import OutboxEvent, Ticket


def test_linked_telegram_chat_can_create_ticket(
    client: TestClient,
    register_client: Callable[[], dict[str, Any]],
) -> None:
    account = register_client()
    link = client.post("/api/v1/users/me/telegram/link-token", headers=account["headers"])
    assert link.status_code == 200
    confirmed = client.post(
        "/api/v1/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        json={
            "update_id": 41,
            "message": {
                "chat": {"id": 200002, "type": "private"},
                "text": "/start " + link.json()["token"],
            },
        },
    )
    assert confirmed.json()["accepted"]

    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        json={
            "update_id": 42,
            "message": {
                "chat": {"id": 200002, "type": "private"},
                "text": "/new Printer failure | The office printer does not respond at all",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Ticket)) >= 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) >= 1


def test_telegram_webhook_rejects_invalid_secret(client: TestClient) -> None:
    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"update_id": 43},
    )

    assert response.status_code == 401
