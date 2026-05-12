"""Admin user-management router.

Mounted at ``/api/v1/admin/users``. Currently exposes a single
recovery endpoint — ``POST /merge`` — used by a superadmin to fold
one ``users`` row into another. Built primarily for the pre-launch
case where an early version of the Google SSO callback inserted a
duplicate row at an email that already had a local-password user.

Auth: ``orgs.manage`` (which superadmins short-circuit). The
operation rewrites identifying data so we want the highest gate
we have today.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import User
from app.rate_limit import get_client_ip
from app.schemas.admin_users import UserMergeRequest, UserMergeResponse
from app.services import audit_service, user_merge_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])


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
