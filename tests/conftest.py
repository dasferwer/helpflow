from collections.abc import Callable, Generator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from helpflow.config import get_settings
from helpflow.db import engine
from helpflow.main import app
from helpflow.seed import seed_database


@pytest.fixture(scope="session", autouse=True)
def reset_database() -> Generator[None, None, None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE notification_deliveries, outbox_events, ticket_history, "
                "comments, tickets, users RESTART IDENTITY CASCADE"
            )
        )
    seed_database()
    yield


@pytest.fixture(scope="session")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="session")
def operator_headers(client: TestClient) -> dict[str, str]:
    settings = get_settings()
    return _login(
        client,
        str(settings.operator_email),
        settings.operator_password.get_secret_value(),
    )


@pytest.fixture
def register_client(client: TestClient) -> Callable[[], dict[str, Any]]:
    def factory() -> dict[str, Any]:
        email = f"client-{uuid4()}@example.com"
        password = "StrongPass123!"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": "Test Client", "password": password},
        )
        assert response.status_code == 201
        return {
            "id": response.json()["id"],
            "email": email,
            "headers": _login(client, email, password),
        }

    return factory


@pytest.fixture
def create_ticket(client: TestClient) -> Callable[[dict[str, str]], dict[str, Any]]:
    def factory(headers: dict[str, str]) -> dict[str, Any]:
        response = client.post(
            "/api/v1/tickets",
            headers=headers,
            json={
                "subject": "Production access problem",
                "description": "The user cannot open the protected application section.",
                "priority": "high",
            },
        )
        assert response.status_code == 201
        return response.json()

    return factory
