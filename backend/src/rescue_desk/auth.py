from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.config import get_settings
from rescue_desk.database import get_db
from rescue_desk.models import Membership, Role, User

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")


@dataclass(frozen=True)
class Principal:
    user_id: str
    organization_id: str
    role: Role
    email: str
    display_name: str


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    return password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    return password_hash.verify(password, stored_hash)


def create_access_token(*, user: User, membership: Membership) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": user.id,
        "org": membership.organization_id,
        "role": membership.role.value,
        "email": user.email,
        "name": user.display_name,
        "exp": expires_at,
        "iat": datetime.now(UTC),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, expires_at


def decode_access_token(token: str) -> Principal:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = str(payload["sub"])
        organization_id = str(payload["org"])
        role = Role(str(payload["role"]))
        email = str(payload["email"])
        display_name = str(payload["name"])
    except (InvalidTokenError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return Principal(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        email=email,
        display_name=display_name,
    )


def get_current_principal(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> Principal:
    principal = decode_access_token(token)
    membership = db.scalar(
        select(Membership).where(
            Membership.organization_id == principal.organization_id,
            Membership.user_id == principal.user_id,
            Membership.role == principal.role,
        )
    )
    user = db.get(User, principal.user_id)
    if membership is None or user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive account")
    return principal


def require_roles(*allowed: Role) -> Callable[..., Principal]:
    def dependency(
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency
