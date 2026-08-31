from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient


def test_client_can_link_telegram_chat(
    client: TestClient,
    register_client: Callable[[], dict[str, Any]],
) -> None:
    account = register_client()
    response = client.put(
        "/api/v1/users/me/telegram",
        headers=account["headers"],
        json={"chat_id": "100001"},
    )

    assert response.status_code == 200
    assert response.json()["telegram_chat_id"] == "100001"


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/tickets")

    assert response.status_code == 401
