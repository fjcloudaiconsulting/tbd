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

The two surfaces share a router by design so the in-app URL space
stays flat (``/api/v1/admin/users`` for everything user-shaped), but
they sit on independent service modules:

- ``user_merge_service``: mutating recovery flow.
- ``admin_users_search_service``: read-only list/detail.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.admin_users import UserMergeRequest, UserMergeResponse
from app.services import (
    admin_users_search_service,
    admin_users_service,
    audit_service,
    user_merge_service,
)
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


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
    payload = await admin_users_search_service.list_users(
        db,
        q=q,
        org_filter=org_id,
        role_filter=role,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )

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


@router.delete("/{user_id}", status_code=status.HTTP_200_OK)
async def delete_user(
    user_id: int,
    request: Request,
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
    await audit_service.record_audit_event(
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
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "target_user_id": snapshot["id"],
            "target_email": snapshot["email"],
            "target_username": snapshot["username"],
            "target_org_id": snapshot["org_id"],
            "fk_cleanup_counts": result["fk_cleanup_counts"],
        },
    )

    return {
        "deleted_user_id": snapshot["id"],
        "fk_cleanup_counts": result["fk_cleanup_counts"],
    }
