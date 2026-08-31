from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from helpflow.db import get_db
from helpflow.models import User, UserRole
from helpflow.security import decode_access_token

DbSession = Annotated[Session, Depends(get_db)]
bearer = HTTPBearer(auto_error=False)


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise error
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = UUID(str(payload.get("sub")))
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise error from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_staff(current_user: CurrentUser) -> User:
    if current_user.role not in {UserRole.OPERATOR, UserRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff role required")
    return current_user


StaffUser = Annotated[User, Depends(require_staff)]
