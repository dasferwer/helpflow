from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient


def test_ticket_visibility_assignment_and_workflow(
    client: TestClient,
    operator_headers: dict[str, str],
    register_client: Callable[[], dict[str, Any]],
    create_ticket: Callable[[dict[str, str]], dict[str, Any]],
) -> None:
    owner = register_client()
    outsider = register_client()
    ticket = create_ticket(owner["headers"])

    hidden = client.get(f"/api/v1/tickets/{ticket['id']}", headers=outsider["headers"])
    assert hidden.status_code == 404

    invalid_transition = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        headers=owner["headers"],
        json={"status": "closed"},
    )
    assert invalid_transition.status_code == 409

    staff = client.get("/api/v1/users/staff", headers=operator_headers).json()
    operator = next(user for user in staff if user["role"] == "operator")
    assigned = client.post(
        f"/api/v1/tickets/{ticket['id']}/assign",
        headers=operator_headers,
        json={"assignee_id": operator["id"]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "in_progress"

    resolved = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        headers=operator_headers,
        json={"status": "resolved", "reason": "Access restored"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_at"] is not None

    closed = client.post(
        f"/api/v1/tickets/{ticket['id']}/transition",
        headers=owner["headers"],
        json={"status": "closed"},
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_internal_comment_is_hidden_from_client(
    client: TestClient,
    operator_headers: dict[str, str],
    register_client: Callable[[], dict[str, Any]],
    create_ticket: Callable[[dict[str, str]], dict[str, Any]],
) -> None:
    owner = register_client()
    ticket = create_ticket(owner["headers"])

    forbidden = client.post(
        f"/api/v1/tickets/{ticket['id']}/comments",
        headers=owner["headers"],
        json={"body": "Private investigation note", "is_internal": True},
    )
    assert forbidden.status_code == 403

    internal = client.post(
        f"/api/v1/tickets/{ticket['id']}/comments",
        headers=operator_headers,
        json={"body": "Internal diagnostic details", "is_internal": True},
    )
    assert internal.status_code == 201

    public = client.post(
        f"/api/v1/tickets/{ticket['id']}/comments",
        headers=operator_headers,
        json={"body": "Please retry the operation", "is_internal": False},
    )
    assert public.status_code == 201

    details = client.get(
        f"/api/v1/tickets/{ticket['id']}",
        headers=owner["headers"],
    )
    assert details.status_code == 200
    assert [comment["body"] for comment in details.json()["comments"]] == [
        "Please retry the operation"
    ]
