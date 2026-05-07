"""Tenant org-management router (Track D).

Mounted at ``/api/v1/orgs``. Houses owner-scoped operations on the
caller's own organization: rename today, more later (transfer
ownership, etc.). Strictly distinct from:

- ``/api/v1/admin/orgs`` (``admin_orgs.py``) — platform-superadmin
  cross-tenant management.
- ``/api/v1/orgs/data`` (``org_data.py``) — owner-only destructive
  data reset on the caller's own org.
- ``/api/v1/orgs/members`` (``org_members.py``) — member roster.

The audit pattern follows the architect's PR-C decisions for
org-delete: stage the success row in the request-scoped session so
it commits atomically with the business write; record failure rows
on an independent session so a rolled-back business txn still leaves
a forensic trail.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.org_permissions import require_org_owner
from app.database import get_db
from app.deps import get_session_factory
from app.models.user import Organization, User
from app.rate_limit import get_client_ip
from app.schemas.orgs import OrgRenameRequest, OrgResponse
from app.services import audit_service, org_service

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


@router.patch("/{org_id}/rename", response_model=OrgResponse)
async def rename_org_endpoint(
    org_id: int,
    body: OrgRenameRequest,
    request: Request,
    current_user: User = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Rename the caller's own organization.

    OWNER-only. Path ``org_id`` MUST match ``current_user.org_id`` —
    cross-tenant attempts are 403 even for the owner of some other
    org. Same-name submissions short-circuit to a no-op (200, no
    audit row, no DB write). Duplicate names (case-insensitive)
    return 409 with a generic message that does not reveal the
    conflicting org's identity.
    """
    if org_id != current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot rename other organizations",
        )

    # Snapshot identity-shaping fields BEFORE any await on db that
    # could rollback or swap the session. Lazy-loading current_user
    # attributes after a rollback can trip MissingGreenlet on the
    # async engine, same gotcha that bit the org-delete failure-path
    # audit before PR #152.
    actor_user_id = current_user.id
    actor_email = current_user.email
    target_org_id = org_id
    attempted_name = body.name

    try:
        old_name, new_name = await org_service.rename_org(
            db, org_id=target_org_id, new_name=attempted_name,
        )

        if old_name == new_name:
            # No-op: same canonical name. No audit row, no DB write.
            # Release the FOR UPDATE lock cleanly via rollback before
            # re-fetching the row in the autocommit path below. The
            # final SELECT returns the unchanged row.
            await db.rollback()
            org = (
                await db.execute(
                    select(Organization).where(Organization.id == target_org_id)
                )
            ).scalar_one()
            return OrgResponse.model_validate(org)

        # Stage the audit row in the request-scoped session so it
        # commits in the same txn as the rename. The Organization
        # row is NOT deleted here, so the FK on ``audit_events
        # .target_org_id`` is satisfied at INSERT time and stays
        # satisfied forever (unlike the org-delete path, which
        # needed the audit row to survive a cascade-NULL).
        audit_service.add_audit_event_to_session(
            db,
            event_type="org.rename",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=target_org_id,
            target_org_name=new_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={"old_name": old_name, "new_name": new_name},
        )

        try:
            await db.commit()
        except IntegrityError:
            # Race: another rename slipped between our preflight and
            # commit and won the UNIQUE constraint. Translate to a
            # generic 409 (no cross-tenant name disclosure) and let
            # the failure-path audit fire below.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An organization with that name already exists",
            )
    except HTTPException:
        # Best-effort rollback. If the failure originated in the
        # service preflight (404/409), the session is already
        # otherwise clean; if it was the IntegrityError translation
        # above, the rollback already happened. Either way, calling
        # rollback() again is a no-op on a clean session.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 — defensive, never bubble.
            pass
        # Failure-path audit on an INDEPENDENT session so the row
        # survives the business rollback. Mirrors the org-delete
        # failure pattern. Captures the attempted name in both
        # ``target_org_name`` and ``detail.attempted_name`` for
        # forensic value when the rename was rejected.
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.rename",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=target_org_id,
            target_org_name=attempted_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={"attempted_name": attempted_name},
        )
        raise

    await logger.ainfo(
        "org.rename",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=target_org_id,
        old_name=old_name,
        new_name=new_name,
    )

    org = (
        await db.execute(
            select(Organization).where(Organization.id == target_org_id)
        )
    ).scalar_one()
    return OrgResponse.model_validate(org)
