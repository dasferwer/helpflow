import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from helpflow.models import TelegramLink, TelegramReceipt, User
from helpflow.schemas import (
    TelegramLinkResponse,
    TelegramUpdate,
    TelegramWebhookResponse,
    TicketCreate,
)
from helpflow.services import create_ticket


def issue_link(db: Session, user: User) -> TelegramLinkResponse:
    db.refresh(user, with_for_update=True)
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    link = db.get(TelegramLink, user.id)
    if link is None:
        link = TelegramLink(user_id=user.id)
        db.add(link)
    link.token_hash = hashlib.sha256(token.encode()).hexdigest()
    link.expires_at = expiry
    db.commit()
    return TelegramLinkResponse(token=token, expires_at=expiry)


def handle_update(db: Session, payload: TelegramUpdate) -> TelegramWebhookResponse:
    fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": payload.update_id})
    previous = db.get(TelegramReceipt, payload.update_id)
    if previous is not None:
        if previous.fingerprint != fingerprint:
            raise HTTPException(
                status_code=409, detail="update_id уже использован с другим содержимым"
            )
        return TelegramWebhookResponse.model_validate(previous.response)
    result = apply_update(db, payload)
    db.add(
        TelegramReceipt(
            update_id=payload.update_id,
            fingerprint=fingerprint,
            response=result.model_dump(mode="json"),
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "users_telegram_chat_id_key"
        ):
            raise HTTPException(
                status_code=409, detail="Чат уже привязан к другой учётной записи"
            ) from exc
        raise
    return result


def apply_update(db: Session, payload: TelegramUpdate) -> TelegramWebhookResponse:
    message = payload.message
    if message is None or not message.text or message.chat.type != "private":
        return TelegramWebhookResponse(
            accepted=False, reason="Нужно текстовое сообщение в личном чате"
        )
    chat_id = str(message.chat.id)
    value = message.text.strip()
    if value.startswith("/start "):
        token_hash = hashlib.sha256(value[7:].strip().encode()).hexdigest()
        link = db.scalar(select(TelegramLink).where(TelegramLink.token_hash == token_hash))
        if link is None:
            return TelegramWebhookResponse(accepted=False, reason="Код недействителен")
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:chat, 203))"), {"chat": chat_id}
        )
        owner = db.scalar(select(User.id).where(User.telegram_chat_id == chat_id))
        if owner is not None and owner != link.user_id:
            return TelegramWebhookResponse(
                accepted=False, reason="Чат уже привязан к другой учётной записи"
            )
        # Сначала пользователь, затем код: такой же порядок используется при выпуске кода.
        user = db.scalar(select(User).where(User.id == link.user_id).with_for_update())
        db.refresh(link, with_for_update=True)
        if (
            user is None
            or not user.is_active
            or link.token_hash != token_hash
            or link.expires_at <= datetime.now(UTC)
        ):
            return TelegramWebhookResponse(accepted=False, reason="Код недействителен")
        user.telegram_chat_id = chat_id
        user.telegram_verified_at = datetime.now(UTC)
        link.expires_at = datetime.now(UTC)
        return TelegramWebhookResponse(accepted=True)
    user = db.scalar(
        select(User).where(
            User.telegram_chat_id == chat_id,
            User.is_active.is_(True),
            User.telegram_verified_at.is_not(None),
        )
    )
    if user is None:
        return TelegramWebhookResponse(accepted=False, reason="Сначала подтвердите привязку чата")
    if not value.startswith("/new ") or "|" not in value:
        return TelegramWebhookResponse(accepted=False, reason="Используйте /new Тема | Описание")
    subject, description = (part.strip() for part in value[5:].split("|", 1))
    try:
        ticket_payload = TicketCreate(subject=subject, description=description)
    except ValidationError:
        return TelegramWebhookResponse(
            accepted=False, reason="Тема или описание не соответствуют ограничениям"
        )
    ticket = create_ticket(db, ticket_payload, user, commit=False)
    return TelegramWebhookResponse(accepted=True, ticket_number=ticket.number)
