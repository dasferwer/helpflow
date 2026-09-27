from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient


def test_client_can_request_code_without_linking_chat(
    client: TestClient,
    register_client: Callable[[], dict[str, Any]],
) -> None:
    account = register_client()
    response = client.post(
        "/api/v1/users/me/telegram/link-token",
        headers=account["headers"],
    )

    assert response.status_code == 200
    assert len(response.json()["token"]) >= 32
    assert (
        client.get("/api/v1/users/me", headers=account["headers"]).json()["telegram_chat_id"]
        is None
    )


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/tickets")

    assert response.status_code == 401
