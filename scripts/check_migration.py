"""Проверить обновление исторических данных в отдельной временной БД."""

import hashlib
import json
import os
import subprocess
from uuid import uuid4

from sqlalchemy import Engine, create_engine, text

from helpflow.db import engine

TABLES = (
    "users",
    "tickets",
    "comments",
    "ticket_history",
    "outbox_events",
    "notification_deliveries",
)
ADDED = (
    "version",
    "telegram_verified_at",
    "payload",
    "attempts",
    "next_attempt_at",
    "lease_until",
    "lease_token",
)


def snapshot(database: Engine) -> str:
    values = {}
    with database.connect() as connection:
        for table in TABLES:
            rows = list(
                connection.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id")).scalars()
            )
            if table in {"users", "tickets", "notification_deliveries"}:
                rows = [
                    {key: value for key, value in row.items() if key not in ADDED} for row in rows
                ]
            values[table] = rows
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def main() -> None:
    if engine.url.database != "helpflow_test":
        raise RuntimeError("Проверка разрешена только из тестового окружения helpflow_test")
    name = "helpflow_migration_" + uuid4().hex
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    target = create_engine(engine.url.set(database=name))
    env = {**os.environ, "HELPFLOW_DATABASE_URL": target.url.render_as_string(hide_password=False)}
    try:
        subprocess.run(["alembic", "upgrade", "20260831_0001"], env=env, check=True)
        with target.begin() as connection:
            user, ticket, event, delivery = [uuid4() for _ in range(4)]
            connection.execute(
                text("""INSERT INTO users
                (id,email,full_name,password_hash,role,is_active,telegram_chat_id)
                VALUES (:id,'migration@example.com','Исторический клиент',
                'test-hash','client',true,'98765')"""),
                {"id": user},
            )
            connection.execute(
                text("""INSERT INTO tickets (id,subject,description,priority,status,client_id)
                VALUES (:id,'Историческая заявка','Проверка сохранности данных',
                'normal','new',:user)"""),
                {"id": ticket, "user": user},
            )
            connection.execute(
                text("""INSERT INTO outbox_events (id,event_type,payload,attempts)
                VALUES (:id,'ticket.created','{}',0)"""),
                {"id": event},
            )
            connection.execute(
                text("""INSERT INTO notification_deliveries
                (id,event_id,event_type,channel,recipient,status,error)
                VALUES (:id,:event,'ticket.created','telegram','98765','failed','legacy')"""),
                {"id": delivery, "event": event},
            )
        before = snapshot(target)
        subprocess.run(["alembic", "upgrade", "head"], env=env, check=True)
        if snapshot(target) != before:
            raise RuntimeError("Исторические данные изменились при миграции")
        with target.connect() as connection:
            assert connection.scalar(text("SELECT version FROM tickets")) == 1
            assert connection.scalar(text("SELECT telegram_verified_at FROM users")) is None
        print("Исторические данные сохранены; старый чат требует подтверждения")
    finally:
        target.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name}"))
        admin.dispose()


if __name__ == "__main__":
    main()
