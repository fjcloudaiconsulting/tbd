"""Tenant org-data router (L3.1) — destructive owner-only operations
on the org's own data."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.auth.org_permissions import require_org_owner
from app.database import get_db
from app.models.user import Organization, User
from app.schemas.org_data import OrgDataResetRequest, OrgDataResetResponse
from app.services import org_data_service

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/orgs/data", tags=["org-data"])


@router.post("/reset", response_model=OrgDataResetResponse)
async def reset_org_data(
    body: OrgDataResetRequest,
    current_user: User = Depends(require_org_owner),
    db: AsyncSession = Depends(get_db),
):
    org = (await db.execute(
        select(Organization).where(Organization.id == current_user.org_id)
    )).scalar_one()

    # Snapshot ORM-bound fields BEFORE the cascade. If the wipe raises
    # and we rollback, accessing org.id / org.name / current_user.email
    # afterward could trigger a lazy reload on the async engine and trip
    # MissingGreenlet — same gotcha as org_members.create_invitation's
    # pre-commit snapshot pattern.
    org_id = org.id
    org_name = org.name
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_role = current_user.role.value

    expected = f"RESET {org_name}"
    if body.confirm_phrase.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm_phrase does not match required value",
        )

    # ``reset_org_data`` commits per batch internally so locks release
    # between chunks. We do NOT issue an outer commit here — there's
    # nothing pending. On exception, rollback whatever was uncommitted
    # at the moment of failure; previously-committed batches persist
    # and the user can re-run the reset (idempotent on both the wipe
    # and the seed) to finish.
    try:
        counts = await org_data_service.reset_org_data(db, org_id=org_id)
    except Exception as e:  # noqa: BLE001 — translate to generic 500 + log.
        await db.rollback()
        await logger.aerror(
            "org.data.reset.failed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            actor_role=actor_role,
            org_id=org_id,
            org_name=org_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset organization data",
        )

    await logger.ainfo(
        "org.data.reset",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        actor_role=actor_role,
        org_id=org_id,
        org_name=org_name,
        deleted_rows_by_table=counts,
    )
    return {"deleted_rows_by_table": counts}
