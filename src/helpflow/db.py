from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from helpflow.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    pool_timeout=5,
    connect_args={
        "connect_timeout": 5,
        "options": "-c lock_timeout=3000 -c statement_timeout=10000",
    },
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
