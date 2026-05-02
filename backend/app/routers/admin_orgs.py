"""Admin org-management router (L4.3).

Mounted at `/api/v1/admin/orgs`. Auth via the platform `orgs.view` /
`orgs.manage` permissions (superadmin short-circuits both today;
fine-grained roles can land later via L4.8 without touching this
file).

Destructive endpoints (DELETE, PUT subscription) emit a single
structlog event — prefix `admin.org.*` — so an operator can later
attribute who did what to whom even before the L4.7 audit table
exists. FK / SQL diagnostics never bleed into 500 bodies — generic
message client-side, full detail server-side.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.auth.feature_catalog import ALL_FEATURE_KEYS
from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_current_user
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import SubscriptionStatus
from app.models.user import User
from app.schemas.admin_orgs import OrgDeleteRequest, SubscriptionUpdateRequest
from app.schemas.feature_override import FeatureOverrideUpsert, OrgFeatureOverrideResponse
from app.services import admin_orgs_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

logger = structlog.stdlib.get_logger()
log = structlog.get_logger()

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin-orgs"])


@router.get(
    "",
    dependencies=[Depends(require_permission("orgs.view"))],
)
async def list_orgs(
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await admin_orgs_service.list_orgs(db, q=q, limit=limit, offset=offset)


@router.get(
    "/{org_id}",
    dependencies=[Depends(require_permission("orgs.view"))],
)
async def get_org_detail(org_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await admin_orgs_service.get_org_detail(db, org_id=org_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")


@router.put("/{org_id}/subscription")
async def update_org_subscription(
    org_id: int,
    body: SubscriptionUpdateRequest,
    current_user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        # Look up name once for the audit event.
        detail = await admin_orgs_service.get_org_detail(db, org_id=org_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    try:
        before, after = await admin_orgs_service.update_subscription(
            db,
            org_id=org_id,
            plan_id=body.plan_id,
            status=SubscriptionStatus(body.status) if body.status else None,
            trial_end=body.trial_end,
            current_period_end=body.current_period_end,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await db.commit()

    await logger.ainfo(
        "admin.org.subscription.override",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=org_id,
        target_org_name=detail["name"],
        before=before,
        after=after,
    )
    return {"before": before, "after": after}


@router.delete("/{org_id}")
async def delete_org(
    org_id: int,
    body: OrgDeleteRequest,
    current_user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
):
    if org_id == current_user.org_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete your own organization",
        )

    try:
        detail = await admin_orgs_service.get_org_detail(db, org_id=org_id)
    except NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if body.confirm_name.strip() != detail["name"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm_name does not match organization name",
        )

    try:
        counts = await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        await db.commit()
    except Exception as e:  # noqa: BLE001 — translate to generic 500 + log.
        await db.rollback()
        await logger.aerror(
            "admin.org.delete.failed",
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=org_id,
            target_org_name=detail["name"],
            error=str(e),
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete organization",
        )

    await logger.ainfo(
        "admin.org.delete",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=org_id,
        target_org_name=detail["name"],
        deleted_rows_by_table=counts,
    )
    return {"deleted": counts}


# ── Feature overrides (L4.11) ─────────────────────────────────────────────


def _validate_feature_key(key: str) -> None:
    if key not in ALL_FEATURE_KEYS:
        raise ValidationError(f"Unknown feature key: {key!r}")


async def _override_to_response(row: OrgFeatureOverride, db: AsyncSession) -> dict:
    """Resolve set_by_email by joining to users."""
    from datetime import datetime

    email = None
    if row.set_by is not None:
        email = await db.scalar(select(User.email).where(User.id == row.set_by))
    is_expired = row.expires_at is not None and row.expires_at <= datetime.utcnow()
    return {
        "feature_key": row.feature_key,
        "value": row.value,
        "set_by": row.set_by,
        "set_by_email": email,
        "set_at": row.set_at.isoformat() if row.set_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "note": row.note,
        "is_expired": is_expired,
    }


@router.put(
    "/{org_id}/feature-overrides/{feature_key}",
    response_model=OrgFeatureOverrideResponse,
)
async def set_feature_override(
    org_id: int,
    feature_key: str,
    body: FeatureOverrideUpsert,
    user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
):
    try:
        _validate_feature_key(feature_key)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    existing = await db.scalar(
        select(OrgFeatureOverride).where(
            OrgFeatureOverride.org_id == org_id,
            OrgFeatureOverride.feature_key == feature_key,
        )
    )
    old_value = existing.value if existing else None
    old_expires_at = existing.expires_at if existing else None

    try:
        async with db.begin_nested():
            if existing is None:
                row = OrgFeatureOverride(
                    org_id=org_id,
                    feature_key=feature_key,
                    value=body.value,
                    set_by=user.id,
                    expires_at=body.expires_at,
                    note=body.note,
                )
                db.add(row)
            else:
                existing.value = body.value
                existing.set_by = user.id
                existing.expires_at = body.expires_at
                existing.note = body.note
                row = existing
        await db.commit()
        await db.refresh(row)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Override changed concurrently; retry.",
        )

    log.info(
        "admin.org.feature.set",
        target_org_id=org_id,
        feature_key=feature_key,
        old_value=old_value,
        new_value=body.value,
        old_expires_at=old_expires_at.isoformat() if old_expires_at else None,
        new_expires_at=body.expires_at.isoformat() if body.expires_at else None,
        actor_user_id=user.id,
        actor_email=user.email,
        note_present=body.note is not None,
    )

    return await _override_to_response(row, db)


@router.delete(
    "/{org_id}/feature-overrides/{feature_key}",
    status_code=204,
)
async def revoke_feature_override(
    org_id: int,
    feature_key: str,
    user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
):
    # 400 if key isn't in the catalog (matches PUT translation).
    try:
        _validate_feature_key(feature_key)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    existing = await db.scalar(
        select(OrgFeatureOverride).where(
            OrgFeatureOverride.org_id == org_id,
            OrgFeatureOverride.feature_key == feature_key,
        )
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"FeatureOverride {org_id}/{feature_key} not found",
        )

    old_value = existing.value
    await db.delete(existing)
    await db.commit()

    log.info(
        "admin.org.feature.revoked",
        target_org_id=org_id,
        feature_key=feature_key,
        old_value=old_value,
        actor_user_id=user.id,
        actor_email=user.email,
    )
    return Response(status_code=204)
