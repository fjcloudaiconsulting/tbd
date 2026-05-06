import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.auth import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
    UserResponse,
)
from app.schemas.user import PasswordChange, ProfileUpdate
from app.security import create_email_verification_token, hash_password, verify_password
from app.services.email_service import send_verification_email

_USERNAME_RE = re.compile(USERNAME_PATTERN)


def _aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC. The `users` step-up expiry column
    is plain `DateTime` (naive) for cross-DB compatibility, but every
    write goes through `datetime.now(timezone.utc)` so the underlying
    instant is always UTC. This helper makes the comparison safe even
    if a future migration flips the column to `DateTime(timezone=True)`.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

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
        password_set=user.password_set,
    )


@router.put("/me", response_model=UserResponse)
async def update_profile(
    body: ProfileUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.username is not None and body.username != current_user.username:
        # Enforce the stricter /register rules only on actual changes so
        # legacy users with a grandfathered short/looser username can
        # still update their other profile fields.
        if (
            len(body.username) < USERNAME_MIN_LENGTH
            or len(body.username) > USERNAME_MAX_LENGTH
            or not _USERNAME_RE.match(body.username)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} "
                    "characters: letters, digits, dot, underscore, or hyphen only."
                ),
            )

        existing = await db.execute(
            select(User).where(User.username == body.username)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already taken",
            )
        current_user.username = body.username

    email_changing = (
        body.email is not None and body.email != current_user.email
    )
    if email_changing:
        # Closes S-P1-2: without re-auth, a session-only compromise could
        # swap the recovery channel to an attacker-controlled inbox and
        # convert a transient hijack into persistent account takeover.
        # Two acceptable proofs of presence:
        #   - normal users (`password_set=True`) supply `current_password`
        #   - SSO users who never set a password (`password_set=False`)
        #     instead supply a fresh `stepup_token` that the SSO step-up
        #     callback wrote on their row (5min hard expiry, single-use).
        if current_user.password_set:
            if not body.current_password or not verify_password(
                body.current_password, current_user.password_hash
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Current password is required and must be correct to change email",
                )
        else:
            now_check = datetime.now(timezone.utc)
            stored = current_user.stepup_token
            expires_at = current_user.stepup_token_expires_at
            # Compare in a constant-time manner; reject missing/expired
            # tokens with the same generic 400 the password branch
            # returns to avoid leaking which check failed.
            valid = (
                bool(body.stepup_token)
                and stored is not None
                and expires_at is not None
                and _aware(expires_at) > now_check
                and secrets.compare_digest(body.stepup_token, stored)
            )
            if not valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Step-up verification with Google is required to change email",
                )
            # Consume the token so it cannot be replayed.
            current_user.stepup_token = None
            current_user.stepup_token_expires_at = None
        existing = await db.execute(
            select(User).where(User.email == body.email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already taken",
            )
        # Capture the new email now (body.email survives the pydantic
        # validation; current_user.email is still the old one until the
        # assignment below).
        new_email = body.email
        current_user.email = new_email
        # New address is unverified by definition; force the user back
        # through the verify-email flow before any trust is granted.
        current_user.email_verified = False
        # Kill every existing access/refresh token. If an attacker
        # already holds one and happened to get the current password,
        # the change is still logged out globally and a real user
        # re-authenticates from scratch.
        current_user.sessions_invalidated_at = datetime.now(timezone.utc)
        # Issue a fresh verification token bound to the new email
        # (S-P2-1) and deliver it in the background so the handler
        # does not block on SMTP.
        token = create_email_verification_token(current_user.id, new_email)
        background_tasks.add_task(send_verification_email, new_email, token)

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
    # Two paths through this handler:
    #   - `password_set=True` (default for every classic register flow):
    #     require a valid `current_password`. Existing behavior.
    #   - `password_set=False` (Google SSO user setting a real password
    #     for the first time): skip the current-password check; any
    #     supplied value is ignored. After the write `password_set`
    #     flips True permanently so subsequent rotations land in the
    #     standard branch above.
    if current_user.password_set:
        if not body.current_password or not verify_password(
            body.current_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )

    now = datetime.now(timezone.utc)
    current_user.password_hash = hash_password(body.new_password)
    current_user.password_set = True
    current_user.password_changed_at = now
    current_user.sessions_invalidated_at = now
    await db.commit()
