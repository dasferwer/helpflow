from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from helpflow.db import Base


class UserRole(StrEnum):
    CLIENT = "client"
    OPERATOR = "operator"
    ADMIN = "admin"


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TicketStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    DEAD = "dead"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class UUIDMixin:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('client', 'operator', 'admin')",
            name="ck_users_valid_role",
        ),
        UniqueConstraint("email", name="users_email_key"),
        UniqueConstraint("telegram_chat_id", name="users_telegram_chat_id_key"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(String(20), default=UserRole.CLIENT, nullable=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    telegram_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Ticket(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'critical')",
            name="ck_tickets_valid_priority",
        ),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'waiting_customer', 'resolved', 'closed')",
            name="ck_tickets_valid_status",
        ),
        UniqueConstraint("number", name="tickets_number_key"),
        Index("ix_tickets_client_created", "client_id", "created_at"),
        Index("ix_tickets_assignee_status", "assignee_id", "status"),
    )

    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    number: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[TicketPriority] = mapped_column(
        String(20), default=TicketPriority.NORMAL, nullable=False
    )
    status: Mapped[TicketStatus] = mapped_column(
        String(30), default=TicketStatus.NEW, nullable=False
    )
    client_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[User] = relationship(foreign_keys=[client_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="ticket", order_by="Comment.created_at"
    )
    history: Mapped[list["TicketHistory"]] = relationship(
        back_populates="ticket", order_by="TicketHistory.created_at"
    )


class Comment(UUIDMixin, Base):
    __tablename__ = "comments"
    __table_args__ = (Index("ix_comments_ticket_created", "ticket_id", "created_at"),)

    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class TicketHistory(UUIDMixin, Base):
    __tablename__ = "ticket_history"
    __table_args__ = (Index("ix_ticket_history_ticket_created", "ticket_id", "created_at"),)

    ticket_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str | None] = mapped_column(String(30))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="history")
    actor: Mapped[User | None] = relationship()


class OutboxEvent(UUIDMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "created_at"),)

    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(UUIDMixin, Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'retry', 'dead', 'sent', 'skipped', 'failed')",
            name="ck_deliveries_valid_status",
        ),
        UniqueConstraint("event_id", "recipient", name="uq_delivery_recipient"),
        Index("ix_delivery_due", "status", "next_attempt_at"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), default="telegram", nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[DeliveryStatus] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelegramReceipt(Base):
    __tablename__ = "telegram_receipts"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class DeliveryReplay(UUIDMixin, Base):
    __tablename__ = "delivery_replays"
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("notification_deliveries.id"), nullable=False
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
