from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.auth import UserResponse
from app.schemas.user import PasswordChange, ProfileUpdate
from app.security import hash_password, verify_password

router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        phone=user.phone,
        avatar_url=user.avatar_url,
        email_verified=user.email_verified,
        role=user.role.value,
        org_id=user.org_id,
        org_name=user.organization.name,
        billing_cycle_day=user.organization.billing_cycle_day,
        is_superadmin=user.is_superadmin,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
    )


@router.put("/me", response_model=UserResponse)
async def update_profile(
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.username is not None and body.username != current_user.username:
        existing = await db.execute(
            select(User).where(User.username == body.username)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken",
            )
        current_user.username = body.username

    if body.email is not None and body.email != current_user.email:
        existing = await db.execute(
            select(User).where(User.email == body.email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already taken",
            )
        current_user.email = body.email

    sent = body.model_fields_set
    if "first_name" in sent:
        current_user.first_name = body.first_name or None
    if "last_name" in sent:
        current_user.last_name = body.last_name or None
    if "phone" in sent:
        current_user.phone = body.phone or None
    if "avatar_url" in sent:
        current_user.avatar_url = body.avatar_url or None

    await db.commit()
    await db.refresh(current_user, ["organization"])

    return _user_response(current_user)


@router.post("/me/password", status_code=204)
async def change_password(
    body: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    now = datetime.now(timezone.utc)
    current_user.password_hash = hash_password(body.new_password)
    current_user.password_changed_at = now
    current_user.sessions_invalidated_at = now
    await db.commit()
