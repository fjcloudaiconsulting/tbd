"""Admin user-management router.

Mounted at ``/api/v1/admin/users``. Exposes:

- ``POST /merge`` (since PR #222): superadmin recovery to fold one
  ``users`` row into another. Built primarily for the pre-launch case
  where an early version of the Google SSO callback inserted a
  duplicate row at an email that already had a local-password user.
  Gated by ``orgs.manage`` because it rewrites identifying data.

- ``GET /`` and ``GET /{user_id}`` (L4.4 slice): cross-org user
  search. Read-only discovery surface so a superadmin can find a user
  across every org. Gated by ``users.view``.

- ``POST /{user_id}/email-change`` and ``DELETE /{user_id}/pending-email``
  (TBD-362): operator recovery for a user who mistyped their address at
  signup and is now locked out — the verification link went somewhere that
  does not exist, and every self-serve remedy mails that same dead address.
  Gated by ``users.reset_credentials``.

  ⚠⚠ THE OPERATOR WRITES EXACTLY ONE COLUMN: ``users.pending_email``. This
  does NOT verify the account. The user still proves control of the new
  address by clicking, through the unchanged
  ``verify_email`` -> ``_promote_pending_email`` path, and stays locked out
  until they do. The ``email_verified`` writer set gains ZERO members,
  pinned by ``tests/auth/test_email_verified_writer_set.py``. Design:
  ``specs/2026-08-23-tbd-362-admin-email-recovery.md``.

  ⚠⚠ ``specs/2026-05-22-l4-4-admin-slices.md:325-348`` ALREADY DESIGNED
  THIS ENDPOINT AND IS BROKEN — it is the obvious prior art and it predates
  TBD-361 by three months. It mints an ``email_verify`` token with the new
  address baked in and never writes ``pending_email``. Implemented
  literally, ``verify_email`` computes ``promoting = False`` (the column is
  NULL) and then refuses because the claim is not ``user.email``, so the
  link **400s on every click, forever** — while the endpoint returns 200
  and dispatches mail, passing any "200 returned, mail sent" test. Do not
  follow that spec.

The surfaces share a router by design so the in-app URL space
stays flat (``/api/v1/admin/users`` for everything user-shaped), but
they sit on independent service modules:

- ``user_merge_service``: mutating recovery flow.
- ``admin_users_search_service``: read-only list/detail.
"""
# ⚠⚠ NO ``from __future__ import annotations`` IN THIS MODULE, and it must
# not be re-added. It was here until TBD-362 and is incompatible with the
# ``@limiter.limit`` decorator on the two email-recovery routes below.
#
# slowapi wraps the handler with a ``functools.wraps``-decorated closure
# defined in ``slowapi/extension.py``. FastAPI resolves a route's parameter
# annotations with ``get_type_hints`` against ``call.__globals__`` -- which is
# the WRAPPER's globals, i.e. slowapi's module, not this one. With the future
# import every annotation is a string, so ``AdminEmailChangeRequest`` and
# ``BackgroundTasks`` fail to resolve there and FastAPI silently demotes both
# parameters to untyped QUERY params: every request 422s with
# ``{"loc": ["query", "body"], "msg": "Field required"}``.
#
# It never bit before because no rate-limited route in a future-importing
# router module took a Pydantic body or ``BackgroundTasks``. Without the
# import the annotations are real objects and nothing has to be resolved.
# ``tests/routers/test_admin_email_change.py`` turns a regression red
# immediately (422 instead of 200) rather than at runtime in production.

import time
from threading import Lock
from typing import Literal, Optional

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.pat import require_interactive_session
from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_session_factory
from app.models.notification import NotificationCategory
from app.models.user import Organization, User
from app.rate_limit import get_client_ip, limiter
from app.schemas.admin_users import (
    AdminEmailChangeRequest,
    AdminEmailChangeResponse,
    AdminPendingEmailCancelResponse,
    UserMergeRequest,
    UserMergeResponse,
)
from app.security import create_email_verification_token
from app.services import (
    admin_users_search_service,
    admin_users_service,
    audit_service,
    email_service,
    notification_service,
    user_merge_service,
)
from app.services.email_service import send_verification_email
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.notification_templates import (
    admin_initiated_email_change_requested as _tpl_admin_email_change,
    admin_initiated_email_change_requested_old_address as _tpl_admin_email_change_old,
)
from app.services.user_service import normalize_email


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])


# ── Audit throttle (process-local) ──────────────────────────────────
#
# The list / detail GETs are issued by a superadmin clicking through
# the admin UI. Without a throttle, an actor scrolling a paginated
# list could spray dozens of ``admin.user.viewed`` audit rows in a
# second. That is useless noise that drowns the genuine signal.
#
# Contract:
#   - Throttle window is 60s.
#   - List views are throttled per ``actor_user_id``.
#   - Detail views are throttled per ``(actor_user_id, target_user_id)``
#     so opening user A then user B writes two rows, but refreshing
#     user A within the window stays quiet.
#   - Throttle state is in-process; restart resets the window. That is
#     acceptable for audit cardinality (cold start writes are the
#     valid first row), and it keeps the read path free of a DB
#     round-trip.
#
# The throttle is intentionally NOT applied to ``POST /merge``. Every
# merge attempt must be auditable.
_AUDIT_THROTTLE_SECONDS = 60.0
_audit_throttle_state: dict[tuple, float] = {}
_audit_throttle_lock = Lock()


def _should_emit_view_audit(key: tuple) -> bool:
    """Return True when this (key) hasn't fired within the window."""
    now = time.monotonic()
    with _audit_throttle_lock:
        last = _audit_throttle_state.get(key)
        if last is not None and (now - last) < _AUDIT_THROTTLE_SECONDS:
            return False
        _audit_throttle_state[key] = now
        # Opportunistic GC: drop entries older than 4x the window so
        # the dict doesn't grow unbounded across long-lived processes.
        cutoff = now - (_AUDIT_THROTTLE_SECONDS * 4)
        stale = [k for k, ts in _audit_throttle_state.items() if ts < cutoff]
        for k in stale:
            _audit_throttle_state.pop(k, None)
    return True


def _reset_audit_throttle_for_tests() -> None:
    """Test helper: clear the in-process throttle dictionary.

    Pure side-effect helper. Production code MUST NOT call this.
    """
    with _audit_throttle_lock:
        _audit_throttle_state.clear()


def _request_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("request_id")


@router.post(
    "/merge",
    response_model=UserMergeResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_interactive_session)],
)
async def merge_users(
    request: Request,
    body: UserMergeRequest,
    actor: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Fold ``source_user_id`` into ``target_user_id``.

    Reassigns every reference (audit events, invitations, feature
    overrides, tags, reset lock) from source to target, then
    deletes source. Same-org only. Writes an ``admin.user.merged``
    audit event on success and ``admin.user.merge.failed`` on
    failure.
    """
    # Snapshot actor identity BEFORE any commit/rollback. SQLAlchemy
    # expires ORM attributes on commit/rollback; subsequent
    # ``actor.id`` / ``actor.email`` access would trigger a lazy
    # load, which raises ``MissingGreenlet`` outside the greenlet
    # context the audit-write opens — turning every error path into
    # a 500 and breaking the success-path audit row too.
    actor_id = actor.id
    actor_email = actor.email

    try:
        counts = await user_merge_service.merge_users(
            db,
            source_user_id=body.source_user_id,
            target_user_id=body.target_user_id,
        )
        await db.commit()
    except NotFoundError as e:
        await db.rollback()
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.merge.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "source_user_id": body.source_user_id,
                "target_user_id": body.target_user_id,
                "reason": "not_found",
                "message": str(e),
            },
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except (ConflictError, ValidationError) as e:
        await db.rollback()
        status_code = (
            status.HTTP_409_CONFLICT
            if isinstance(e, ConflictError)
            else status.HTTP_400_BAD_REQUEST
        )
        reason = "conflict" if isinstance(e, ConflictError) else "validation"
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.merge.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "source_user_id": body.source_user_id,
                "target_user_id": body.target_user_id,
                "reason": reason,
                "message": str(e),
            },
        )
        raise HTTPException(status_code=status_code, detail=str(e))
    except Exception:
        await db.rollback()
        await logger.aexception(
            "admin.user.merge.error",
            source_user_id=body.source_user_id,
            target_user_id=body.target_user_id,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.merge.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "source_user_id": body.source_user_id,
                "target_user_id": body.target_user_id,
                "reason": "internal_error",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="merge failed",
        )

    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.user.merged",
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "source_user_id": body.source_user_id,
            "target_user_id": body.target_user_id,
            "counts": counts,
        },
    )

    return UserMergeResponse(
        source_user_id=body.source_user_id,
        target_user_id=body.target_user_id,
        counts=counts,
    )


# ── Cross-org user search (L4.4 slice) ──────────────────────────────


_STATUS_FILTER = Literal["active", "inactive", "unverified", "superadmin"]
_ROLE_FILTER = Literal["owner", "admin", "member"]


@router.get("")
async def list_users(
    request: Request,
    q: Optional[str] = Query(default=None, max_length=120),
    org_id: Optional[int] = Query(default=None, ge=1),
    role: Optional[_ROLE_FILTER] = Query(default=None),
    status_filter: Optional[_STATUS_FILTER] = Query(default=None, alias="status"),
    sort_by: Optional[str] = Query(default=None),
    sort_dir: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: User = Depends(require_permission("users.view")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Paginated cross-org user list.

    Privacy note: ``q`` is NEVER logged. Only ``query_length`` and
    ``result_count`` go to structlog so a raw search string can't
    leak into the log pipeline.

    Audit: one ``admin.user.list.viewed`` row per actor per minute
    (process-local throttle). The first hit always records.
    """
    try:
        payload = await admin_users_search_service.list_users(
            db,
            q=q,
            org_filter=org_id,
            role_filter=role,
            status_filter=status_filter,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    await logger.ainfo(
        "admin.user.list.viewed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        query_length=len(q) if q else 0,
        org_filter=org_id,
        role_filter=role,
        status_filter=status_filter,
        result_count=len(payload["items"]),
        total=payload["total"],
    )

    if _should_emit_view_audit(("list", actor.id)):
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.list.viewed",
            actor_user_id=actor.id,
            actor_email=actor.email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "query_length": len(q) if q else 0,
                "org_filter": org_id,
                "role_filter": role,
                "status_filter": status_filter,
                "result_count": len(payload["items"]),
                "total": payload["total"],
                "limit": limit,
                "offset": offset,
            },
        )

    return payload


@router.get("/{user_id}")
async def get_user_detail(
    user_id: int,
    request: Request,
    actor: User = Depends(require_permission("users.view")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Full user detail with org memberships + recent audit events.

    Audit: one ``admin.user.viewed`` row per (actor, user_id) per
    minute (process-local throttle).
    """
    try:
        payload = await admin_users_search_service.get_user_detail(
            db, user_id=user_id
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    await logger.ainfo(
        "admin.user.viewed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        target_user_id=user_id,
    )

    if _should_emit_view_audit(("detail", actor.id, user_id)):
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.viewed",
            actor_user_id=actor.id,
            actor_email=actor.email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={"target_user_id": user_id},
        )

    return payload


# ── System-level hard delete ────────────────────────────────────────
#
# Gated by ``users.delete`` (added 2026-05-17). The superadmin
# short-circuit makes this superadmin-only today; the seeded
# role_permissions row keeps the L4.8 role editor accurate.
#
# Precondition errors return 409 with a structured ``detail`` of
# ``{"code": <stable string>, "message": <human-readable>}``. The
# code values are exported from ``admin_users_service`` so the
# frontend can branch without parsing English.


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_interactive_session)],
)
async def delete_user(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    actor: User = Depends(require_permission("users.delete")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Hard-delete a User row.

    Preconditions enforced server-side:

    - actor is not the same user as the target
    - target is not a platform superadmin
    - target.is_active is False

    On a 409 precondition failure, the response body is
    ``{"detail": {"code": ..., "message": ...}}``. On 404 the body
    follows the standard FastAPI shape.
    """
    # Snapshot actor identity before any commit/rollback. Same
    # MissingGreenlet hazard as in ``merge_users``.
    actor_id = actor.id
    actor_email = actor.email

    try:
        result = await admin_users_service.delete_user(
            db,
            target_user_id=user_id,
            actor_user_id=actor_id,
        )
        await db.commit()
    except NotFoundError as e:
        await db.rollback()
        # Idempotency: deleting a user that doesn't exist returns
        # 404. Not an audit-worthy failure on its own (no actor
        # intent to record beyond the request log).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ConflictError as e:
        await db.rollback()
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.delete.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "target_user_id": user_id,
                "code": e.code,
                "reason": e.code or "conflict",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": e.code, "message": str(e)},
        )
    except Exception:
        await db.rollback()
        await logger.aexception(
            "admin.user.delete.error",
            target_user_id=user_id,
            actor_user_id=actor_id,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.user.delete.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=None,
            target_org_name=None,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "target_user_id": user_id,
                "reason": "internal_error",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="delete failed",
        )

    snapshot = result["snapshot"]
    request_ip = get_client_ip(request)
    audit_event_id = await audit_service.record_audit_event(
        session_factory,
        event_type="admin.user.deleted",
        actor_user_id=actor_id,
        actor_email=actor_email,
        # The deleted user's org_id is preserved as a snapshot so the
        # audit row can still be cross-referenced from the org's
        # audit timeline.
        target_org_id=snapshot["org_id"],
        target_org_name=None,
        request_id=_request_id(),
        ip_address=request_ip,
        outcome="success",
        detail={
            "target_user_id": snapshot["id"],
            "target_email": snapshot["email"],
            "target_username": snapshot["username"],
            "target_org_id": snapshot["org_id"],
            "fk_cleanup_counts": result["fk_cleanup_counts"],
        },
    )

    # PR4 (section 1 of the 2nd-arch delta): send the final transactional
    # "account deleted" email to the deleted user's last-known address.
    # NO in-app row — the user is gone. The enqueue lives on the audit
    # SUCCESS path ONLY: ``record_audit_event`` returns the row id on a
    # successful commit and ``None`` when the audit insert/commit failed
    # (it logs + swallows rather than raising). Gating on a non-None id
    # is the locked rule — "after the audit commit only; if the audit
    # write fails, no email task is enqueued." Not in a ``finally``, not
    # before the audit, not in parallel.
    if audit_event_id is not None:
        background_tasks.add_task(
            email_service.send_account_deleted_email,
            snapshot["email"],
            snapshot["username"],
        )

    return {
        "deleted_user_id": snapshot["id"],
        "fk_cleanup_counts": result["fk_cleanup_counts"],
    }


# ── Operator email recovery (TBD-362) ───────────────────────────────────────
#
# THE INCIDENT THIS EXISTS FOR. A user mistypes their address at signup. The
# verification link goes to an inbox that does not exist, so
# `POST /auth/login` 403s them forever (`auth.py`, unconditional — no role,
# environment or settings exemption), `resend_verification_public` re-sends to
# the same stored typo, and `forgot_password` mails the same typo. Before
# this, `email_verified` had NO operator writer anywhere in the admin modules
# and there was no in-app remedy at all.
#
# THE RULING. The operator writes `users.pending_email` and NOTHING else. The
# user proves control by clicking; the existing two-phase path promotes. The
# ticket's DoD asked for an eighth `email_verified` writer and framed that as
# the central security concern; under TBD-361 that concern EVAPORATES rather
# than being managed, because the operator's new power is not "assert a
# proof", it is "make the gesture the locked-out user can no longer make for
# themselves". Fenced by `tests/auth/test_email_verified_writer_set.py`.
#
# ⚠ WHY IT REFUSES AN ALREADY-VERIFIED TARGET (`user_already_verified`).
# Without that guard this ships the platform's FIRST superadmin
# account-takeover primitive, confirmed end to end against a real stack:
# repoint -> click at the attacker's inbox -> `_promote_pending_email` ->
# `POST /auth/forgot-password` at the new address (it matches `User.email` and
# gates only on `is_active`) -> reset token issued -> `reset_password` flips
# `password_set = True` -> attacker logs in and reads the victim's accounts;
# the victim's login at the old address 401s. The chain completes even against
# an SSO-only account, converting it into a password account the attacker
# owns. `users.reset_credentials`, `users.impersonate` and `users.invite` had
# zero call sites before this, so it genuinely would have been the first.
#
# ⚠ THE GUARD IS SAFE BY MEASUREMENT, NOT BY CONSTRUCTION, and that is a
# precondition rather than a proof. `email_verified` is a one-way latch, so
# `email_verified=False` plus the unconditional login 403 implies "never held
# a session" implies "owns no user-created data" -- but ONLY for rows created
# after that gate landed (2026-04-30). Migration 018 added the column
# `nullable=False, server_default="0"` with NO backfill twenty days earlier,
# so older rows were stamped 0 while owning arbitrary financial history. That
# cohort was measured EMPTY on production on 2026-08-24
# (`SELECT COUNT(*) FROM users WHERE email_verified=0 AND is_active=1` -> 0),
# and nothing anywhere writes `email_verified = False`, so no existing row can
# enter it. ⚠ A bulk import, a restore from a pre-2026-04-30 dump, or any
# backfill creating active rows with `email_verified=0` VOIDS this. Re-run the
# query before widening anything here.
#
# ⚠ IT IS NOT TOO COSTLY, because every VERIFIED user has a working self-serve
# path: `PUT /users/me` has two proof-of-presence branches --
# `password_set=True` supplies `current_password`, `password_set=False`
# supplies a `stepup_token` minted by the SSO step-up callback. The population
# this refuses is a genuine two-failure conjunction: lost inbox AND lost
# credential, which is a deliberate, logged database write, not an endpoint.
#
# ⚠ `target_is_superadmin` is NOT in the ticket and is load-bearing: without
# it, superadmin A repoints superadmin B's address at an inbox A controls and
# owns the platform's most privileged account. Precedent:
# `admin_users_service.py`, `admin_org_members_service.py` and
# `invitation_service.py` all refuse to mutate a superadmin.
#
# ⚠ NOT IMPLEMENTED, DELIBERATELY: per-actor rate limiting (not expressible
# against the single `Limiter(key_func=get_client_ip)`; `10/hour` on the IP
# key bounds the same abuse at the same order of magnitude and fails CLOSED
# when two operators share an IP), password reset and MFA reset (out of
# scope -- the shared `users.reset_credentials` permission must not drag the
# whole L4.4 slice in), and any change to `resend_verification_public` (it is
# unauthenticated and username-addressable, so letting a caller choose the
# destination mails a credential to a caller-chosen inbox; under TBD-361 that
# token MOVES `users.email`, which is remote account takeover).


_EMAIL_CHANGE_FAILED = "admin.user.email_change.failed"


async def _record_email_change_failure(
    session_factory: async_sessionmaker[AsyncSession],
    request: Request,
    *,
    actor_id: int,
    actor_email: str,
    target_org_id: Optional[int],
    target_org_name: Optional[str],
    detail: dict,
) -> None:
    """Write the refusal row on the independent session.

    ⚠ Every refusal is audited, including the 404 — diverging from
    ``delete_user``, which deliberately does not audit its 404. The
    difference is the payload: probing THIS path carries an
    attacker-supplied DESTINATION address, where the delete path carries no
    body at all. A row saying only "409" cannot distinguish "this account was
    already locked out" from "an operator just attacked an active
    superadmin", which is why the pre-refusal snapshot rides in ``detail``.
    """
    await audit_service.record_audit_event(
        session_factory,
        event_type=_EMAIL_CHANGE_FAILED,
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="failure",
        detail=detail,
    )


@router.post(
    "/{user_id}/email-change",
    response_model=AdminEmailChangeResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_interactive_session)],
)
@limiter.limit("10/hour")
async def trigger_email_change(
    user_id: int,
    request: Request,
    body: AdminEmailChangeRequest,
    background_tasks: BackgroundTasks,
    actor: User = Depends(require_permission("users.reset_credentials")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Repoint a locked-out account's pending email claim at a new address.

    Writes ``users.pending_email`` and mails a verification link there. Does
    NOT write ``users.email``, ``email_verified`` or
    ``sessions_invalidated_at`` — see the section comment above, and
    ``tests/auth/test_sessions_invalidated_at_allowlist.py``, which is a
    function-granular AST fence over that last column and gains no entry.

    Preconditions, in order, each audited::

        target missing                                 404 user_not_found
        normalized addresses differ                    400 emails_do_not_match
        target.email_verified                          409 user_already_verified
        target.is_superadmin                           409 target_is_superadmin
        not target.is_active                           409 user_inactive
        normalize(new) == normalize(target.email)      409 email_unchanged
        another row holds it                           409 email_already_in_use

    A malformed address never reaches here: ``EmailStr`` makes it a FastAPI
    **422**, so it is unaudited BY CONSTRUCTION and there is no
    ``400 invalid_email`` on the wire.
    """
    # Snapshot actor identity BEFORE any commit/rollback. SQLAlchemy expires
    # ORM attributes on both; a later `actor.id` would lazy-load and raise
    # MissingGreenlet outside the greenlet context the audit write opens,
    # turning every error path into a 500. Same hazard as `merge_users`.
    actor_id = actor.id
    actor_email = actor.email

    new_email_norm = normalize_email(body.new_email)
    confirm_norm = normalize_email(body.new_email_confirm)

    target = await db.scalar(select(User).where(User.id == user_id))

    # Snapshot every target field the refusal rows and the success path need,
    # BEFORE any rollback/commit expires the instance.
    if target is None:
        snapshot: dict = {}
        target_org_id: Optional[int] = None
    else:
        snapshot = {
            "target_email_old": target.email,
            "target_email_verified": target.email_verified,
            "target_is_active": target.is_active,
            "target_is_superadmin": target.is_superadmin,
            "previous_pending_email": target.pending_email,
        }
        target_org_id = target.org_id

    # ⚠ Explicit SELECT, never `target.organization`. `target` is loaded with
    # a plain `select(User)`, so the relationship is unloaded and touching it
    # lazy-loads -- which under asyncio raises MissingGreenlet and 500s the
    # request. Same trap `_promote_pending_email` documents.
    target_org_name: Optional[str] = None
    if target_org_id is not None:
        org_row = await db.scalar(
            select(Organization).where(Organization.id == target_org_id)
        )
        target_org_name = org_row.name if org_row is not None else None

    async def _refuse(code: str, message: str, http_status: int):
        await db.rollback()
        await _record_email_change_failure(
            session_factory,
            request,
            actor_id=actor_id,
            actor_email=actor_email,
            target_org_id=target_org_id,
            target_org_name=target_org_name,
            detail={
                "target_user_id": user_id,
                "code": code,
                "attempted_email": new_email_norm,
                "reason": body.reason,
                **snapshot,
            },
        )
        return HTTPException(
            status_code=http_status, detail={"code": code, "message": message}
        )

    if target is None:
        raise await _refuse(
            "user_not_found", "User not found", status.HTTP_404_NOT_FOUND
        )

    # ⚠ Compared AFTER normalization on BOTH sides. A byte comparison rejects
    # `Foo@x.com` against `foo@x.com`, which is the SAME address -- and an
    # operator who learns the two fields must match byte-for-byte starts
    # pasting the first into the second, which defeats the whole confirmation.
    if new_email_norm != confirm_norm:
        raise await _refuse(
            "emails_do_not_match",
            "The two addresses do not match",
            status.HTTP_400_BAD_REQUEST,
        )

    if snapshot["target_email_verified"]:
        raise await _refuse(
            "user_already_verified",
            (
                "This account's email is already verified. A verified user "
                "changes their own address in Settings; this endpoint exists "
                "only for an account locked out of an unverified inbox."
            ),
            status.HTTP_409_CONFLICT,
        )

    if snapshot["target_is_superadmin"]:
        raise await _refuse(
            "target_is_superadmin",
            "A platform superadmin's email cannot be changed here",
            status.HTTP_409_CONFLICT,
        )

    if not snapshot["target_is_active"]:
        raise await _refuse(
            "user_inactive",
            "This account is deactivated",
            status.HTTP_409_CONFLICT,
        )

    # ⚠ NORMALIZED ON BOTH SIDES, and this closes a real defect rather than
    # being tidiness. `verify_email` computes `promoting` BEFORE any equality
    # check, so repointing to the target's OWN current address takes the
    # PROMOTING branch: it writes `sessions_invalidated_at` and a
    # `user.email.changed` audit row whose `old_email == new_email` -- a false
    # completion record for a change that did not happen. This endpoint would
    # be the FIRST writer able to reach that state (`PUT /users/me` cannot:
    # `email_changing` normalises both sides, so a self-addressed change is
    # False and the cancel branch handles it instead), which is exactly why
    # the guard belongs here.
    #
    # ⚠ A BYTE comparison here is not merely imprecise, it is WRONG in two
    # different ways depending on the database, and the SQLite shards see
    # only the harmless one. Mixed-case `users.email` rows genuinely exist in
    # production (the pre-TBD-361 request path wrote `body.email` raw). For a
    # stored `Foo@Bar.com` and an operator typing `foo@bar.com`: on MySQL's
    # `utf8mb4_0900_ai_ci` the advisory SELECT below matches the target's own
    # row and returns a misleading `email_already_in_use`; on SQLite it
    # matches nothing and the endpoint ARMS the self-referential promotion
    # described above.
    if new_email_norm == normalize_email(snapshot["target_email_old"]):
        raise await _refuse(
            "email_unchanged",
            "That is already the address on the account",
            status.HTTP_409_CONFLICT,
        )

    # Advisory only, and deliberately kept: it is a courtesy so the operator
    # is not left waiting on a link that could never work. The BINDING check
    # re-runs at promotion time, up to 24 hours later, with an
    # `IntegrityError` -> 409 backstop.
    #
    # ⚠ MODELLED ON THE PROMOTE-TIME SELECT, which carries `User.id != id` --
    # NOT on `users.py`'s request-time version, which has no id guard and is
    # the shape the `email_unchanged` guard above exists to refuse.
    #
    # ⚠ Do NOT add a unique index on `pending_email` to "fix" this. CLAUDE.md
    # forbids it: a unique constraint on a self-asserted address does not
    # prevent the collision that matters (a claim equal to somebody's LIVE
    # `users.email`) and hands out an address-squatting primitive.
    #
    # ⚠⚠ THE `User.id != user_id` GUARD IS UNFENCEABLE ON THE SQLITE SHARDS
    # AND STILL LOAD-BEARING. Deleting it was MEASURED green across the whole
    # TBD-362 suite. `email_unchanged` above shadows it for any
    # lowercase-stored row, and the case it does NOT shadow needs MySQL:
    # `utf8mb4_0900_ai_ci` is accent-INsensitive as well as case-insensitive
    # while `normalize_email` only lowercases, so `jose@x.com` against a
    # stored `josé@x.com` clears `email_unchanged` and then matches the
    # target's OWN row here -- reporting `email_already_in_use` for the
    # account's own address. SQLite compares binary and sees none of it.
    taken = await db.scalar(
        select(User).where(User.email == new_email_norm, User.id != user_id)
    )
    if taken is not None:
        raise await _refuse(
            "email_already_in_use",
            "Another account already uses that address",
            status.HTTP_409_CONFLICT,
        )

    target.pending_email = new_email_norm
    await db.commit()

    # ⚠ MINT FROM THE NORMALIZED STORED VALUE, never from `body.new_email`.
    # The promote-time guard compares the token's claim to the stored column
    # BYTE-EXACTLY, so minting from raw input yields a link that 400s forever
    # for any operator who typed mixed case.
    #
    # ⚠ `admin_initiated=True` is the claim-provenance carrier. The
    # `user_already_verified` guard above reads the flag at TRIGGER time and
    # this link redeems up to 24 hours later; `_promote_pending_email` refuses
    # the token if the row became verified in between. See
    # `tests/auth/test_admin_email_change_provenance.py`.
    #
    # ⚠ ``user_id`` (the path param), NOT ``target.id``. Identical value, but
    # ``target`` is EXPIRED by the commit above; the read only happens to be
    # safe because ``database.async_session`` sets ``expire_on_commit=False``,
    # and relying on that is the MissingGreenlet trap this module warns about
    # three times. Every read after the commit in this handler is a plain
    # Python value captured beforehand.
    token = create_email_verification_token(
        user_id, new_email_norm, admin_initiated=True
    )
    background_tasks.add_task(send_verification_email, new_email_norm, token)

    # ⚠ `target_org_id` is read off the TARGET and MUST be set. The reserved
    # contract specifies it, and `/admin/audit`'s only org filter is
    # `target_org_id` -- an implementer copying `merge_users`, which passes
    # None, produces rows invisible to every org-scoped audit query.
    #
    # ⚠ `actor_email` is the SUPERADMIN's, diverging from the self-initiated
    # convention. `audit_events` has no `target_user_id` column, so
    # `actor_email` is the only identity column; on an admin-triggered row the
    # reader's question is "which operator", and the target's old address
    # survives in `detail`.
    #
    # `target_email_old` matters because promotion OVERWRITES `users.email`
    # and nothing else preserves the typo. `previous_pending_email` matters
    # because this write IS the "overwrite by a later request" clearer, so
    # without it a destroyed claim leaves no trace anywhere.
    audit_event_id = await audit_service.record_audit_event(
        session_factory,
        event_type="admin.user.email_change.triggered",
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "target_user_id": user_id,
            "target_email_old": snapshot["target_email_old"],
            "target_pending_email": new_email_norm,
            "previous_pending_email": snapshot["previous_pending_email"],
            "reason": body.reason,
            # The claim now standing on this row carries an admin-initiated
            # token. There is no column for that (deliberately -- see
            # `create_email_verification_token`), so the audit trail is the
            # only durable record of the claim's provenance.
            "kind": "admin_initiated",
        },
    )

    # In-app SECURITY row, gated on a non-None audit id: `record_audit_event`
    # returns the row id on a successful commit and None when the audit write
    # failed. Gating on it is the locked rule at `delete_user` -- after the
    # audit commit only, never in a `finally`, never before, never in
    # parallel.
    if audit_event_id is not None:
        title, notif_body, link_url = _tpl_admin_email_change(
            actor_email=actor_email, pending_email=new_email_norm
        )
        await notification_service.dispatch_notification_best_effort(
            db,
            user_id=user_id,
            category=NotificationCategory.SECURITY,
            event_type="admin.user.email_change.triggered",
            title=title,
            body=notif_body,
            link_url=link_url,
            audit_event_id=audit_event_id,
        )

    # ⚠ The OLD-ADDRESS ALERT IS UNCONDITIONAL -- it is not gated on the audit
    # write, and it fires even though that address is by definition
    # unverified. That looks wasteful and is not: "typo'd" and
    # "attacker-chosen" are indistinguishable to the system, and wherever the
    # address is in fact live this is the ONLY out-of-band signal the target
    # gets. Reads use string snapshots, never `target.*`: the best-effort
    # dispatch above may have rolled back, which expires ORM instances.
    alert_title, alert_body, alert_link = _tpl_admin_email_change_old(
        actor_email=actor_email, pending_email=new_email_norm
    )
    await notification_service.send_security_email_best_effort(
        db,
        user_id=user_id,
        email=snapshot["target_email_old"],
        event_type="admin.user.email_change.triggered",
        title=alert_title,
        body=alert_body,
        link_url=alert_link,
    )

    return AdminEmailChangeResponse(
        user_id=user_id,
        email=snapshot["target_email_old"],
        email_verified=False,
        pending_email=new_email_norm,
        previous_pending_email=snapshot["previous_pending_email"],
    )


@router.delete(
    "/{user_id}/pending-email",
    response_model=AdminPendingEmailCancelResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_interactive_session)],
)
@limiter.limit("10/hour")
async def cancel_admin_pending_email(
    user_id: int,
    request: Request,
    actor: User = Depends(require_permission("users.reset_credentials")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Clear a pending email claim on another user's row. Idempotent.

    ⚠ THIS IS NOT OPTIONAL, and the typed double entry on the POST does not
    make it so: that only prevents a MISTYPED address, not a mistyped
    CORRECTION. If the operator mistypes the correction and mails a live
    promotion link to an attacker-owned inbox, the remedies WITHOUT this
    endpoint are: wait out the 24h window with a live takeover link in a
    stranger's inbox; overwrite with a third address, which revokes the bad
    link only by minting another one at an address the operator by hypothesis
    does not have; or direct SQL.

    ⚠ "Just overwrite the claim with the target's own ``users.email``, which
    is inert" is WRONG and was refuted by execution. That write makes
    ``promoting`` evaluate True for any live register-minted bootstrap token,
    so the click drives the FULL promotion path: ``sessions_invalidated_at``
    set for a change that did not happen, a ``user.email.changed`` row with
    ``old_email == new_email``, and two "your email changed" notices to the
    same inbox. It manufactures a false completion record.

    ⚠ Returns **200** with a body where the user-side sibling returns 204.
    Deliberate: the operator needs to know whether anything was cleared.

    ⚠ NO precondition beyond existence — not ``target_is_superadmin``, not
    ``user_already_verified``. Clearing a claim is strictly de-escalating: it
    can only ever revoke a live promotion link, never create one. Refusing on
    those grounds would stop an operator defusing a MISTARGETED claim, which
    is the exact incident this endpoint exists for.
    """
    actor_id = actor.id
    actor_email = actor.email

    target = await db.scalar(select(User).where(User.id == user_id))
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "User not found"},
        )

    previous_pending = target.pending_email
    target_org_id = target.org_id
    org_row = await db.scalar(
        select(Organization).where(Organization.id == target_org_id)
    )
    target_org_name = org_row.name if org_row is not None else None

    if previous_pending is None:
        return AdminPendingEmailCancelResponse(cleared=False)

    # None, never "": an empty string still satisfies `is not None` in the
    # promotion guard and would serialize as a live pending change.
    target.pending_email = None
    await db.commit()

    # Audited only when something was ACTUALLY cleared. A no-op cancel is not
    # a state transition and there is nothing to reconstruct from it; the
    # response's `cleared: false` is the caller's answer.
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.user.email_change.cancelled",
        actor_user_id=actor_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "target_user_id": user_id,
            "previous_pending_email": previous_pending,
        },
    )

    return AdminPendingEmailCancelResponse(cleared=True)
