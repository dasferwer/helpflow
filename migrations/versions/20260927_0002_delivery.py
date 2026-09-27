"""Добавить повторные доставки, версии заявок и подтверждение Telegram-чата."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20260927_0002"
down_revision = "20260831_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_verified_at", sa.DateTime(timezone=True)))
    op.create_table(
        "delivery_replays",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "delivery_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("notification_deliveries.id"),
            nullable=False,
        ),
        sa.Column("actor_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("tickets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.drop_constraint("notification_deliveries_event_id_key", "notification_deliveries")
    op.drop_constraint("ck_deliveries_valid_status", "notification_deliveries")
    op.create_unique_constraint(
        "uq_delivery_recipient", "notification_deliveries", ["event_id", "recipient"]
    )
    op.create_check_constraint(
        "ck_deliveries_valid_status",
        "notification_deliveries",
        "status IN ('pending','sending','retry','dead','sent','skipped','failed')",
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("payload", pg.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("notification_deliveries", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.add_column("notification_deliveries", sa.Column("lease_token", pg.UUID(as_uuid=True)))
    op.create_index("ix_delivery_due", "notification_deliveries", ["status", "next_attempt_at"])
    op.create_table(
        "telegram_links",
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "telegram_receipts",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("response", pg.JSONB(), nullable=False),
    )


def downgrade() -> None:
    # После появления нескольких адресатов возврат к одной записи потерял бы данные.
    raise RuntimeError("Для отката восстановите резервную копию; используйте исправляющую миграцию")
