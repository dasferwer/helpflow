import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from helpflow.config import get_settings

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(key)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded_hash.split("$", maxsplit=5)
        if algorithm != "scrypt":
            return False
        expected_bytes = _decode(expected)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_bytes),
        )
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: UUID, role: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
