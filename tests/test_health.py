from fastapi.testclient import TestClient


def test_health_checks_database_and_rabbitmq(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "rabbitmq": "ok"}
    assert response.headers["X-Request-ID"]
