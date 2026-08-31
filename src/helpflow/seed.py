import logging

from sqlalchemy import func, select

from helpflow.config import get_settings
from helpflow.db import SessionLocal
from helpflow.models import User, UserRole
from helpflow.security import hash_password, verify_password

logger = logging.getLogger(__name__)


def _upsert_staff(
    *,
    email: str,
    password: str,
    full_name: str,
    role: UserRole,
) -> None:
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if user is None:
            db.add(
                User(
                    email=email.lower(),
                    full_name=full_name,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
            return
        user.full_name = full_name
        user.role = role
        user.is_active = True
        if not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)


def seed_database() -> None:
    settings = get_settings()
    _upsert_staff(
        email=str(settings.admin_email),
        password=settings.admin_password.get_secret_value(),
        full_name="HelpFlow Administrator",
        role=UserRole.ADMIN,
    )
    _upsert_staff(
        email=str(settings.operator_email),
        password=settings.operator_password.get_secret_value(),
        full_name="HelpFlow Operator",
        role=UserRole.OPERATOR,
    )
    logger.info("Seed completed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_database()
