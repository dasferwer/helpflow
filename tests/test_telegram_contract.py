"""Контракт Telegram проверяется локально, без отправки сообщений."""

import io
import json
import urllib.error
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from helpflow.worker import notification as worker


def response(monkeypatch, body, status=200):
    monkeypatch.setattr(
        worker, "settings", SimpleNamespace(telegram_bot_token=SecretStr("secret-bot-token"))
    )

    @contextmanager
    def open_request(request, timeout):
        assert timeout == 10
        assert request.get_method() == "POST"
        assert json.loads(request.data) == {"chat_id": "123", "text": "Уведомление"}
        encoded = json.dumps(body).encode()
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "Ошибка", {}, io.BytesIO(encoded)
            )
        yield io.BytesIO(encoded)

    monkeypatch.setattr(worker.urllib.request, "urlopen", open_request)


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        "raw-secret-body",
        {},
        {"ok": "true"},
        {"ok": 1},
        {"ok": True},
        {"ok": True, "result": []},
        {"ok": False, "error_code": [403]},
        {"ok": False, "error_code": "secret-bot-token"},
        {"ok": False, "error_code": True},
    ],
)
def test_malformed_response_has_stable_safe_error(monkeypatch, body):
    response(monkeypatch, body)
    with pytest.raises(worker.DeliveryError) as error:
        worker._send_telegram("123", "Уведомление")
    assert str(error.value) == "invalid_response"
    assert not error.value.permanent


@pytest.mark.parametrize("status", [200, 429])
def test_429_retry_after_is_respected_without_body_storage(monkeypatch, status):
    response(
        monkeypatch,
        {
            "ok": False,
            "error_code": 429,
            "description": "secret-bot-token",
            "parameters": {"retry_after": 120},
        },
        status,
    )
    with pytest.raises(worker.DeliveryError) as error:
        worker._send_telegram("123", "Уведомление")
    assert str(error.value) == "telegram_429"
    assert error.value.retry_after == 120
    assert not error.value.permanent


@pytest.mark.parametrize("delay,expected", [(True, 0), ("120", 0), (-1, 0), (999999, 86400)])
def test_invalid_or_excessive_retry_after_is_bounded(monkeypatch, delay, expected):
    response(monkeypatch, {"ok": False, "error_code": 429, "parameters": {"retry_after": delay}})
    with pytest.raises(worker.DeliveryError) as error:
        worker._send_telegram("123", "Уведомление")
    assert error.value.retry_after == expected


@pytest.mark.parametrize("code,permanent", [(400, True), (403, True), (404, True), (500, False)])
def test_error_classification(monkeypatch, code, permanent):
    response(monkeypatch, {"ok": False, "error_code": code, "description": "secret body"}, code)
    with pytest.raises(worker.DeliveryError) as error:
        worker._send_telegram("123", "Уведомление")
    assert str(error.value) == f"telegram_{code}"
    assert error.value.permanent is permanent


def test_successful_message(monkeypatch):
    response(monkeypatch, {"ok": True, "result": {"message_id": 1}})
    worker._send_telegram("123", "Уведомление")
