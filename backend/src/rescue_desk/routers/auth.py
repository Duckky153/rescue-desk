from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from rescue_desk.auth import (
    Principal,
    create_access_token,
    get_current_principal,
    verify_password,
)
from rescue_desk.database import get_db
from rescue_desk.models import Membership, User
from rescue_desk.schemas import CurrentUserResponse, LoginRequest, TokenResponse

router = APIRouter(prefix="/v1/auth", tags=["authentication"])


@router.post("/token", response_model=TokenResponse)
def login(payload: LoginRequest, db: Annotated[Session, Depends(get_db)]) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    query = select(Membership).where(Membership.user_id == user.id)
    if payload.organization_id:
        query = query.where(Membership.organization_id == payload.organization_id)
    memberships = list(db.scalars(query))
    if not memberships:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No organization access")
    if len(memberships) > 1 and payload.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Select an organization before signing in",
        )
    membership = memberships[0]
    token, expires_at = create_access_token(user=user, membership=membership)
    return TokenResponse(
        access_token=token,
        expires_at=expires_at,
        organization_id=membership.organization_id,
        role=membership.role,
    )


@router.get("/me", response_model=CurrentUserResponse)
def me(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        organization_id=principal.organization_id,
        role=principal.role,
    )
