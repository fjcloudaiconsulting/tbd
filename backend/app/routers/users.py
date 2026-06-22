import re
import secrets
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip, limiter
from app.schemas.auth import (
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    USERNAME_PATTERN,
    UserResponse,
)
from app.models.notification import NotificationCategory
from app.schemas.user import PasswordChange, ProfileUpdate
from app.security import create_email_verification_token, hash_password, verify_password
from app.services import audit_service, notification_service
from app.services.email_service import send_verification_email
from app.services.notification_templates import (
    user_email_changed as _tpl_user_email_changed,
    user_password_changed as _tpl_user_password_changed,
)


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")

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
        is_founder=user.is_founder,
        mfa_enabled=user.mfa_enabled,
        password_set=user.password_set,
        onboarded_at=user.onboarded_at.isoformat() if user.onboarded_at else None,
        allow_manual_balance_adjustment=user.organization.allow_manual_balance_adjustment,
    )


@router.put("/me", response_model=UserResponse)
@limiter.limit("5/hour")
async def update_profile(
    request: Request,
    body: ProfileUpdate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
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
    # Snapshot the old email BEFORE the mutation so the post-commit
    # audit row carries the OLD address. The new address goes into
    # detail.new_email — there's no `target_user_email` column on
    # audit_events today, so the user-target identity is carried via
    # actor_email (self) + detail.
    old_email_for_audit = current_user.email
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
        # Validate the target email BEFORE consuming the step-up token
        # or any password-branch side effects. If the email is already
        # taken the change cannot apply, so the proof of presence must
        # remain usable for the user's retry. (Finding 3 from PR #138.)
        existing = await db.execute(
            select(User).where(User.email == body.email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already taken",
            )
        if not current_user.password_set:
            # Step-up was validated above; only consume now that the
            # change is actually about to be applied.
            current_user.stepup_token = None
            current_user.stepup_token_expires_at = None
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

    if email_changing:
        # Audit AFTER the business commit succeeds. Independent-session
        # write — a failure here does not roll back the email change.
        # PR3 of the notification train uses this row as the trigger
        # source for the user.email.changed in-app + email notification.
        # No target_user_id column on audit_events today; the actor
        # (self) carries the user identity, and the OLD email goes in
        # actor_email so a future "who was this" lookup after a malicious
        # email swap can recover the original address. New email lives
        # in detail.new_email.
        audit_event_id = await audit_service.record_audit_event(
            session_factory,
            event_type="user.email.changed",
            actor_user_id=current_user.id,
            actor_email=old_email_for_audit,
            target_org_id=current_user.org_id,
            target_org_name=current_user.organization.name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "old_email": old_email_for_audit,
                "new_email": current_user.email,
            },
        )

        # PR3: dispatch the security notification AFTER the audit row
        # commits. The recipient is the actor (self) — the audit
        # convention uses ``actor_user_id`` for self-target events.
        # The NEW email is interpolated into the body so the recipient
        # can confirm the change at a glance.
        if audit_event_id is not None:
            title, body, link_url = _tpl_user_email_changed(
                new_email=current_user.email
            )
            await notification_service.dispatch_notification(
                db,
                user_id=current_user.id,
                category=NotificationCategory.SECURITY,
                event_type="user.email.changed",
                title=title,
                body=body,
                link_url=link_url,
                audit_event_id=audit_event_id,
            )
            await db.commit()

    return _user_response(current_user)


@router.post("/me/password", status_code=204)
@limiter.limit("5/hour")
async def change_password(
    request: Request,
    body: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # Two paths through this handler:
    #   - `password_set=True` (default for every classic register flow):
    #     require a valid `current_password`. Existing behavior.
    #   - `password_set=False` (Google SSO user setting a real password
    #     for the first time): require a valid `stepup_token` issued by
    #     the SSO step-up callback. Same proof-of-presence the email
    #     change branch uses, for the same reason — without it a
    #     stolen SSO session could write a persistent local password
    #     and convert a transient hijack into permanent account access.
    #     (Finding 1 from PR #138.) After the write `password_set`
    #     flips True permanently so subsequent rotations land in the
    #     standard branch above.
    # Snapshot whether this is a first-time password set (SSO user) or
    # a rotation (classic register flow) BEFORE the mutation flips the
    # flag — the audit row needs the pre-mutation value.
    was_initial_password_set = not current_user.password_set
    if current_user.password_set:
        if not body.current_password or not verify_password(
            body.current_password, current_user.password_hash
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )
    else:
        now_check = datetime.now(timezone.utc)
        stored = current_user.stepup_token
        expires_at = current_user.stepup_token_expires_at
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
                detail="Step-up verification with Google is required to set a password",
            )
        # Consume the token so it cannot be replayed against any
        # other step-up-gated endpoint.
        current_user.stepup_token = None
        current_user.stepup_token_expires_at = None

    now = datetime.now(timezone.utc)
    current_user.password_hash = hash_password(body.new_password)
    current_user.password_set = True
    current_user.password_changed_at = now
    current_user.sessions_invalidated_at = now
    await db.commit()

    # Audit AFTER the business commit succeeds. PR3 of the notification
    # train uses this row as the trigger source for the
    # user.password.changed security notification (always-on email).
    # Failure paths above raise HTTPException before reaching this
    # point — failure-path auditing is intentionally not added in this
    # PR (separate scope per the audit-gap-closures task).
    audit_event_id = await audit_service.record_audit_event(
        session_factory,
        event_type="user.password.changed",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=current_user.org_id,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"password_set_initial": was_initial_password_set},
    )

    # PR3 of the notification train: dispatch the in-app notification
    # AFTER the audit row commits. ``record_audit_event`` returns the
    # new row's id on success and ``None`` on failure; we skip the
    # notification when audit failed so the forensic trail stays
    # consistent (architect-locked ordering — audit IS the trigger).
    if audit_event_id is not None:
        title, body, link_url = _tpl_user_password_changed()
        await notification_service.dispatch_notification(
            db,
            user_id=current_user.id,
            category=NotificationCategory.SECURITY,
            event_type="user.password.changed",
            title=title,
            body=body,
            link_url=link_url,
            audit_event_id=audit_event_id,
        )
        await db.commit()
