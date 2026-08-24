import re
import secrets
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.pat import require_interactive_session
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.user import Organization, User
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
from app.services.user_service import normalize_email
from app.services.notification_templates import (
    user_email_change_requested as _tpl_user_email_change_requested,
    user_email_change_requested_old_address as _tpl_user_email_change_requested_old_address,
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
        pending_email=user.pending_email,
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


@router.put(
    "/me",
    response_model=UserResponse,
    dependencies=[Depends(require_interactive_session)],
)
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

    # TBD-361. Normalize BOTH sides before comparing. Without this a pure
    # case change (`Foo@Bar.com` against a stored `foo@bar.com`) reads as a
    # change and starts a whole two-phase flow for the same address --
    # re-demanding the password and mailing a pointless confirmation link.
    new_email_norm = normalize_email(body.email) if body.email is not None else None
    email_changing = (
        new_email_norm is not None and new_email_norm != normalize_email(current_user.email)
    )
    # Submitting your CURRENT address while a claim is live cancels the
    # claim. Without this the natural undo gesture silently does nothing and
    # the mistyped address stays clickable for its full 24 hours -- whoever
    # owns it can promote themselves onto the account. No re-auth: cancelling
    # only restores the status quo, it cannot move the recovery channel.
    #
    # ⚠ Reachable by API clients only. The settings form re-seeds its input
    # from `user.email` on every refresh and omits `email` from the payload
    # when it is unchanged, so the browser never transmits this. The UI
    # escape is DELETE /users/me/pending-email.
    cancelling_pending = (
        new_email_norm is not None
        and not email_changing
        and current_user.pending_email is not None
    )
    if cancelling_pending:
        current_user.pending_email = None
    # Snapshot the old email BEFORE the mutation so the post-commit
    # audit row carries the OLD address. The new address goes into
    # detail — there's no `target_user_email` column on audit_events
    # today, so the user-target identity is carried via actor_email
    # (self) + detail. Since TBD-361 the request-time row carries
    # `pending_email`, not `new_email`: at that moment nothing has
    # changed, and the completion row is written at promotion instead.
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
        #
        # ⚠ ADVISORY ONLY since TBD-361, and deliberately kept anyway: it is
        # a courtesy so the user is not left waiting for a link that could
        # never work. The binding check is re-run at PROMOTION time, because
        # 24 hours can pass between claim and proof.
        existing = await db.execute(
            select(User).where(User.email == new_email_norm)
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
        # TBD-361. TWO-PHASE COMMIT. Record the CLAIM; change nothing about
        # identity. The live `email`, `email_verified` and the user's session
        # all survive until the new address proves itself.
        #
        # This handler used to assign `current_user.email` here, clear
        # `email_verified`, and set `sessions_invalidated_at` -- logging the
        # user out in the same request that invalidated their verification.
        # Since every recovery path mails `user.email`, which was now the
        # typo, and `reset_password` never writes `email_verified`, a single
        # mistyped character destroyed the account and its whole financial
        # history with no way back on a solo org.
        #
        # ⚠ Mint from `pending_email`, NOT from `body.email`. They differ
        # whenever the user types mixed case, and the promote-time guard
        # compares the token's claim to the STORED value exactly. Minting
        # from the raw input yields a link that 400s forever, for the one
        # user who typed `Foo@Bar.com`.
        current_user.pending_email = new_email_norm
        # String snapshot for the post-commit notification block: a
        # best-effort dispatch that rolls back expires ORM instances, so
        # reading ``current_user.pending_email`` after it would lazy-load and
        # turn a swallowed failure into a 500.
        pending_email_snapshot = new_email_norm
        token = create_email_verification_token(
            current_user.id, current_user.pending_email
        )
        background_tasks.add_task(
            send_verification_email, current_user.pending_email, token
        )

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

    # Materialize the response NOW, off the freshly-committed instance. The
    # best-effort notification block below may roll back on dispatch failure,
    # which expires the ORM instance; building the response here keeps the
    # return value independent of that and avoids a post-rollback lazy-load.
    user_response = _user_response(current_user)

    if email_changing:
        # Audit AFTER the business commit succeeds. Independent-session
        # write — a failure here does not roll back the claim.
        #
        # ⚠ TBD-361. This is `change_requested`, NOT `changed`. Nothing about
        # the user's identity has changed yet: `users.email` still holds the
        # old address and will keep holding it unless and until the new one
        # is proven. Writing a `user.email.changed` row here would assert a
        # completed change that may never happen — and its `detail.new_email`
        # used to read `current_user.email`, which no longer moves, so the
        # row would claim the address changed to itself.
        #
        # The completion event is written by `auth.verify_email` on the
        # promoting branch, sourced from the promoted value.
        #
        # No target_user_id column on audit_events today; the actor
        # (self) carries the user identity, and the OLD email goes in
        # actor_email so a future "who was this" lookup after a malicious
        # email swap can recover the original address.
        audit_event_id = await audit_service.record_audit_event(
            session_factory,
            event_type="user.email.change_requested",
            actor_user_id=current_user.id,
            actor_email=old_email_for_audit,
            target_org_id=current_user.org_id,
            target_org_name=current_user.organization.name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "old_email": old_email_for_audit,
                "pending_email": current_user.pending_email,
            },
        )

        # Dispatch the security notification AFTER the audit row commits.
        # The recipient is the actor (self) — the audit convention uses
        # ``actor_user_id`` for self-target events.
        #
        # ⚠ TBD-361. ONE channel here, not two. The old single-phase code
        # mailed BOTH addresses: an alert to the old one and a "this address
        # is now your login email" confirmation to the new one. Under the
        # two-phase design that confirmation is simply false at this moment —
        # nothing has changed, and it would arrive in the same instant as the
        # verification link for a change that has not happened, telling the
        # new inbox two contradictory stories. The confirmation moves to the
        # promotion branch in ``auth.verify_email``, where it is true.
        #
        # What stays is the alert to the CURRENT address, and under this
        # design it is worth more than it used to be: the reader still
        # controls the login address, the change has not happened, and they
        # can cancel it. The single-phase version could only tell a victim
        # they had already lost the account.
        if audit_event_id is not None:
            # Snapshot the recipient id BEFORE the best-effort dispatch: on
            # failure the wrapper rolls back and expires ORM instances, so
            # even a post-wrapper ``current_user.id`` read would lazy-load.
            recipient_user_id = current_user.id
            title, body, link_url = _tpl_user_email_change_requested(
                pending_email=pending_email_snapshot
            )
            await notification_service.dispatch_notification_best_effort(
                db,
                user_id=recipient_user_id,
                category=NotificationCategory.SECURITY,
                event_type="user.email.change_requested",
                title=title,
                body=body,
                link_url=link_url,
                audit_event_id=audit_event_id,
            )

            # Both reads below use string snapshots rather than
            # ``current_user.*``: the best-effort dispatch above may have
            # rolled back on failure, which expires ORM instances, so a
            # post-wrapper attribute access would trigger a lazy-load and
            # turn a swallowed dispatch failure back into a 500.
            alert_title, alert_body, alert_link = (
                _tpl_user_email_change_requested_old_address(
                    pending_email=pending_email_snapshot
                )
            )
            await notification_service.send_security_email_best_effort(
                db,
                user_id=recipient_user_id,
                email=old_email_for_audit,
                event_type="user.email.change_requested",
                title=alert_title,
                body=alert_body,
                link_url=alert_link,
            )

    return user_response


@router.delete(
    "/me/pending-email",
    status_code=204,
    dependencies=[Depends(require_interactive_session)],
)
@limiter.limit("10/hour")
async def cancel_pending_email(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Abandon a pending email change (TBD-361). Idempotent.

    ⚠⚠ NO password and NO step-up, deliberately, and this is not an
    oversight of the S-P1-2 re-auth gate on the change itself. Requesting a
    change moves the account's recovery channel and therefore demands proof
    of presence; cancelling one only restores the status quo and can move
    nothing. Demanding the password to undo a mistake is the exact shape
    that made the original defect unrecoverable — a user who mistyped their
    address and cannot reach their inbox must not also need to remember a
    password to get out of it. **TBD-362 added the audit row below and
    deliberately did NOT add re-auth here; do not "finish the job".**

    Idempotent 204 rather than 404 on "nothing pending": the caller's goal
    is a state, not a transition, and a user clicking Cancel twice has not
    made an error.
    """
    if current_user.pending_email is None:
        # No state change, so nothing to record. Auditing the no-op would let
        # any live session spray rows into `/admin/audit` at `10/hour` with
        # no corresponding change for a reader to reconstruct.
        return Response(status_code=204)

    # Snapshot everything the post-commit audit needs BEFORE the commit: the
    # commit expires the instance and `current_user.organization` would
    # lazy-load, which under asyncio raises MissingGreenlet.
    cancelled_pending_email = current_user.pending_email
    actor_user_id = current_user.id
    actor_email = current_user.email
    org_id = current_user.org_id
    # ⚠ Explicit SELECT, never `current_user.organization`: `get_current_user`
    # loads the row with a plain `select(User)`, so the relationship is
    # unloaded. Same trap `_promote_pending_email` documents.
    org_row = await db.scalar(
        select(Organization).where(Organization.id == org_id)
    )
    org_name = org_row.name if org_row is not None else None

    # None, never "": an empty string still satisfies `is not None` in
    # the promotion guard, and would serialize into the response as a
    # pending change, rendering an empty row in the UI.
    current_user.pending_email = None
    await db.commit()

    # TBD-362 §6. This endpoint wrote NO audit row at all until now, which
    # left a real gap: a live session — INCLUDING A HIJACKED ONE — could void
    # a pending claim with nothing whatsoever in `/admin/audit`, and the
    # request-time `user.email.change_requested` row would stand forever as
    # the unresolved half of a story.
    #
    # ⚠ The justification is NOT "the admin endpoint's target's only
    # defence". That was an earlier draft's reasoning and it is refuted:
    # every target of `POST /admin/users/{id}/email-change` is unverified, so
    # they 403 at login and cannot reach this interactive-session-gated route
    # at all. That population cannot cancel. The row is right for every OTHER
    # caller, which is the general case.
    #
    # Independent session, after the business commit: a failure here must not
    # roll back the cancel. `actor_email` is the user's own (self-target
    # convention), and the destroyed address rides in `detail` because this
    # write is what erases it.
    await audit_service.record_audit_event(
        session_factory,
        event_type="user.email.change_cancelled",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"cancelled_pending_email": cancelled_pending_email},
    )
    return Response(status_code=204)


@router.post(
    "/me/password",
    status_code=204,
    dependencies=[Depends(require_interactive_session)],
)
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

    # Audit AFTER the business commit succeeds. This row is the trigger
    # source for the user.password.changed security notification
    # (always-on email).
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

    # Dispatch the in-app notification AFTER the audit row commits.
    # ``record_audit_event`` returns the
    # new row's id on success and ``None`` on failure; we skip the
    # notification when audit failed so the forensic trail stays
    # consistent (architect-locked ordering — audit IS the trigger).
    if audit_event_id is not None:
        # Snapshot the recipient BEFORE the best-effort dispatch: on failure
        # the wrapper rolls back, which expires ORM instances, so a later
        # ``current_user.email`` read would lazy-load and re-raise as a 500.
        recipient_user_id = current_user.id
        recipient_email = current_user.email
        title, body, link_url = _tpl_user_password_changed()
        await notification_service.dispatch_notification_best_effort(
            db,
            user_id=current_user.id,
            category=NotificationCategory.SECURITY,
            event_type="user.password.changed",
            title=title,
            body=body,
            link_url=link_url,
            audit_event_id=audit_event_id,
        )

        # Dual-channel: email the account's current address AFTER the
        # in-app row commits (outside its savepoint). Force-on +
        # best-effort — a raising mailer never fails the request, rolls
        # back the password change, or rolls back the in-app row.
        await notification_service.send_security_email_best_effort(
            db,
            user_id=recipient_user_id,
            email=recipient_email,
            event_type="user.password.changed",
            title=title,
            body=body,
            link_url=link_url,
        )
