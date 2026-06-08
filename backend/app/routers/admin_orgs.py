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

from datetime import datetime
from typing import Optional

from app._time import utcnow_naive

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.auth.feature_catalog import ALL_FEATURE_KEYS
from app.auth.permissions import require_permission
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.feature_override import OrgFeatureOverride
from app.models.notification import NotificationCategory
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.user import Organization, Role, User
from app.rate_limit import get_client_ip
from app.schemas.admin_orgs import (
    AdminMemberResponse,
    AdminMemberUpdateRequest,
    OrgDeleteRequest,
    SubscriptionUpdateRequest,
)
from app.schemas.feature_override import FeatureOverrideUpsert, OrgFeatureOverrideResponse
from app.schemas.feature_state import FeatureStateResponse
from app.services import (
    admin_org_members_service,
    admin_orgs_service,
    audit_service,
    feature_service,
    notification_service,
)
from app.services.exceptions import ConflictError, NotFoundError, ValidationError
from app.services.notification_templates import (
    admin_org_plan_changed as _tpl_admin_org_plan_changed,
)

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin-orgs"])


# Cap on the number of per-row entries embedded in the
# `admin.feature_override.expired_swept` audit detail. 50 is a
# reasonable ceiling for a JSON column and keeps the audit row
# readable in the admin UI; over the cap, the audit row carries
# `truncated_count` and `counts_by_feature` so the aggregate signal
# survives even if individual rows fall off.
_SWEEP_AUDIT_ENTRY_CAP = 50


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware (L4.9)."""
    return structlog.contextvars.get_contextvars().get("request_id")


@router.get(
    "",
    dependencies=[Depends(require_permission("orgs.view"))],
)
async def list_orgs(
    q: str | None = Query(default=None, max_length=120),
    sort_by: str | None = Query(default=None),
    sort_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await admin_orgs_service.list_orgs(
            db, q=q, sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail) from exc


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
    request: Request,
    current_user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
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
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.org.subscription.override",
        actor_user_id=current_user.id,
        actor_email=current_user.email,
        target_org_id=org_id,
        target_org_name=detail["name"],
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"before": before, "after": after},
    )

    # Plan-change-specific audit row (audit-gap closure PR2 of the
    # notification train). The generic `admin.org.subscription.override`
    # event above covers any field change on the subscription — plan,
    # status, trial_end, current_period_end — but PR3 of the
    # notification train needs a clean trigger source that fires
    # ONLY when the plan_id actually changes (status flips don't notify
    # org members about a plan change). Emitting both rows lets the
    # generic compliance log keep its coverage AND the future
    # notification dispatcher key off the specific event_type without
    # parsing detail.before/detail.after.
    if "plan_id" in before:
        audit_event_id = await audit_service.record_audit_event(
            session_factory,
            event_type="admin.org.plan.changed",
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=org_id,
            target_org_name=detail["name"],
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={
                "old_plan_id": before["plan_id"],
                "new_plan_id": after["plan_id"],
            },
        )

        # PR3 of the notification train: fan out the in-app
        # ``org_admin`` notification to every active org admin of the
        # affected org. Architect-locked recipient set is OWNER ∪
        # ADMIN; the actor is included (a superadmin operator may also
        # be an org admin elsewhere, but here the broadcast is scoped
        # to ``org_id`` so the operator-self exclusion isn't a concern).
        if audit_event_id is not None:
            # Resolve the new plan's display name so the notification
            # body carries human copy rather than a numeric id. Best
            # effort — fall back to a stable placeholder if the lookup
            # fails so the notification still ships.
            new_plan_name = "Updated plan"
            new_plan_id = after.get("plan_id")
            if new_plan_id is not None:
                plan_row = (
                    await db.execute(
                        select(Plan).where(Plan.id == new_plan_id)
                    )
                ).scalar_one_or_none()
                if plan_row is not None:
                    new_plan_name = plan_row.name

            title, body, link_url = _tpl_admin_org_plan_changed(
                new_plan_name=new_plan_name,
                actor_email=current_user.email,
            )
            await notification_service.dispatch_notification_to_org_admins(
                db,
                org_id=org_id,
                category=NotificationCategory.ORG_ADMIN,
                event_type="admin.org.plan.changed",
                title=title,
                body=body,
                link_url=link_url,
                audit_event_id=audit_event_id,
            )
            await db.commit()
    return {"before": before, "after": after}


@router.delete("/{org_id}")
async def delete_org(
    org_id: int,
    body: OrgDeleteRequest,
    request: Request,
    current_user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
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

    # Snapshot plain Python scalars from current_user BEFORE the try
    # block. After db.rollback() in the except branch the ORM session
    # expires all loaded objects; accessing current_user.id on a dead
    # async session raises MissingGreenlet and swallows the audit write.
    actor_id: int = current_user.id
    actor_email: str = current_user.email

    # Snapshot the org's identifying fields BEFORE the delete so the
    # durable audit row carries answers to "what was deleted" even
    # after the FK ON DELETE SET NULL cascade nulls target_org_id.
    # Bounded — counts not member lists, no PII beyond what the audit
    # table already accepts.
    org_snapshot = {
        "org_id": org_id,
        "org_name": detail["name"],
        "created_at": detail.get("created_at"),
        "billing_cycle_day": detail.get("billing_cycle_day"),
        "member_count_at_delete": len(detail.get("members") or []),
        "subscription": detail.get("subscription"),
        "deleted_by_user_id": actor_id,
        "deleted_by_email": actor_email,
    }

    try:
        # 1) Stage the success audit row FIRST so the FK
        #    (target_org_id → organizations.id) still resolves at
        #    INSERT time — the org row is still present.
        # 2) Run delete_org_cascade. The cascade includes the org row
        #    DELETE; the FK is ON DELETE SET NULL, so the audit row's
        #    target_org_id is nulled by the DB at the same time.
        # 3) Update the audit row's `detail` with the deleted_rows
        #    counts and commit. All atomic — audit row exists iff
        #    delete succeeded.
        # Snapshot in `detail.snapshot` preserves the org's identity
        # after the cascade nulls target_org_id, which is the whole
        # point of writing this row before the delete.
        audit_row = audit_service.add_audit_event_to_session(
            db,
            event_type="admin.org.delete",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=org_id,
            target_org_name=detail["name"],
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail={"snapshot": org_snapshot},
        )
        # Flush so the audit row is INSERTed with the FK still
        # satisfiable (org row still present), before the cascade
        # tears the org away.
        await db.flush()
        counts = await admin_orgs_service.delete_org_cascade(db, org_id=org_id)
        # Reassign the JSON detail (SQLAlchemy doesn't track in-place
        # dict mutation on JSON columns by default).
        audit_row.detail = {
            "snapshot": org_snapshot,
            "deleted_rows_by_table": counts,
        }
        await db.commit()
    except Exception as e:  # noqa: BLE001 — translate to generic 500 + log.
        await db.rollback()
        # Use pre-snapshotted scalars — current_user is expired after
        # rollback on an async session and accessing .id would raise
        # MissingGreenlet, swallowing the audit write below.
        await logger.aerror(
            "admin.org.delete.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=org_id,
            target_org_name=detail["name"],
            error=str(e),
            error_type=type(e).__name__,
        )
        # Independent-session audit write — the business txn has been
        # rolled back (taking the staged success row with it), but the
        # failure must still appear in the audit log. That's the whole
        # point of opening a fresh session for the audit row.
        await audit_service.record_audit_event(
            session_factory,
            event_type="admin.org.delete.failed",
            actor_user_id=actor_id,
            actor_email=actor_email,
            target_org_id=org_id,
            target_org_name=detail["name"],
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="failure",
            detail={
                "snapshot": org_snapshot,
                "error": str(e),
                "error_type": type(e).__name__,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete organization",
        )

    await logger.ainfo(
        "admin.org.delete",
        actor_user_id=actor_id,
        actor_email=actor_email,
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
    email = None
    if row.set_by is not None:
        email = await db.scalar(select(User.email).where(User.id == row.set_by))
    is_expired = row.expires_at is not None and row.expires_at <= utcnow_naive()
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


@router.post("/feature-overrides/sweep-expired")
async def sweep_expired_feature_overrides(
    request: Request,
    user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Delete every ``org_feature_overrides`` row whose ``expires_at``
    is in the past. Idempotent (returns ``deleted_count: 0`` when nothing
    matches). Audit row is always written.

    Audit detail (PR-C / PR #141 #1) includes a bounded summary of the
    rows that were deleted so ops can answer "which orgs/features lost
    access" without reaching for structlog. ``entries`` is capped at
    ``_SWEEP_AUDIT_ENTRY_CAP``; over the cap, ``truncated_count`` and
    ``counts_by_feature`` carry the rest.

    On the rare ``DELETE`` rowcount mismatch path (locked N rows under
    SELECT FOR UPDATE, deleted M < N) the audit detail records
    ``deleted_count`` and ``locked_count`` plus a ``divergence`` flag,
    and OMITS per-row ``entries`` and ``counts_by_feature``: without
    ``DELETE ... RETURNING`` (unsupported on MySQL) we cannot honestly
    tell which of the locked rows our DELETE removed versus rows a
    concurrent actor removed, so we refuse to guess.
    """
    cutoff = utcnow_naive()
    # Lock-then-delete-by-id so the audit summary describes rows this
    # sweep actually removed, not rows it merely observed.
    #
    # The previous SELECT-then-DELETE-by-predicate pattern was racy:
    # two overlapping sweeps could each snapshot the same expired rows
    # (predicate-equal), the first DELETE removed them, and the second
    # DELETE matched nothing yet still audited
    # ``deleted_count = len(snapshot)`` plus per-row entries for rows
    # it never touched. With FOR UPDATE on InnoDB the second sweep
    # blocks until the first commits, then locks zero rows; with
    # DELETE-by-id and rowcount-driven counts, even a non-locking
    # dialect (e.g. tests on SQLite) can't drift out of sync because
    # the audit numbers come from the actual rows removed.
    locked_rows = (
        await db.execute(
            select(OrgFeatureOverride)
            .where(OrgFeatureOverride.expires_at.is_not(None))
            .where(OrgFeatureOverride.expires_at <= cutoff)
            .order_by(OrgFeatureOverride.org_id, OrgFeatureOverride.feature_key)
            .with_for_update()
        )
    ).scalars().all()

    # Snapshot identity for the audit detail BEFORE the delete; after
    # commit the in-memory rows are detached and SQLAlchemy's
    # expire-on-commit would invalidate attribute access.
    locked_by_id = {
        row.id: {
            "org_id": row.org_id,
            "feature_key": row.feature_key,
            "value": row.value,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }
        for row in locked_rows
    }
    locked_ids = list(locked_by_id.keys())

    deleted_count = 0
    divergence = False
    if locked_ids:
        delete_result = await db.execute(
            delete(OrgFeatureOverride).where(OrgFeatureOverride.id.in_(locked_ids))
        )
        affected = delete_result.rowcount
        if affected is None or affected == len(locked_ids):
            # Happy path on every locking dialect we care about.
            # ``rowcount`` of -1/None on a dialect that doesn't report
            # affected rows is treated as the all-locked-deleted case;
            # FOR UPDATE held the rows in InnoDB so this is safe.
            deleted_count = len(locked_ids)
        else:
            # Divergence: rowcount disagrees with the locked snapshot.
            # On MySQL InnoDB with SELECT FOR UPDATE this branch
            # should never fire in practice; it's a defensive
            # fallback for environments without row locks (e.g., the
            # SQLite test database) and protects audit detail from
            # claiming false row identities.
            #
            # We refuse to guess which of the locked rows our DELETE
            # actually removed: without ``DELETE ... RETURNING``
            # (which MySQL does not support) the answer is
            # unknowable. ``deleted_count`` (the rowcount) is
            # authoritative; ``entries`` and ``counts_by_feature``
            # are deliberately omitted from the audit detail.
            divergence = True
            deleted_count = affected
            await logger.awarning(
                "admin.feature_override.sweep.lock_delete_mismatch",
                locked_count=len(locked_ids),
                deleted_count=affected,
            )

    # Audit detail. Happy path: bounded per-row entries +
    # counts_by_feature for ops spot-checks. Divergence path:
    # counts only, with an explicit flag and note so a future reader
    # of the audit table can tell why per-row identity is missing.
    detail: dict[str, object]
    if divergence:
        detail = {
            "deleted_count": deleted_count,
            "locked_count": len(locked_ids),
            "divergence": True,
            "divergence_reason": "concurrent_modification",
            "note": (
                "Exact row identities could not be determined under "
                "concurrent sweep activity. deleted_count is "
                "authoritative; counts_by_feature and entries are "
                "omitted."
            ),
        }
    else:
        counts_by_feature: dict[str, int] = {}
        entries: list[dict] = []
        for row_id in locked_ids:
            snap = locked_by_id[row_id]
            counts_by_feature[snap["feature_key"]] = (
                counts_by_feature.get(snap["feature_key"], 0) + 1
            )
            if len(entries) < _SWEEP_AUDIT_ENTRY_CAP:
                entries.append(snap)
        truncated_count = max(0, deleted_count - _SWEEP_AUDIT_ENTRY_CAP)
        detail = {
            "deleted_count": deleted_count,
            "entries": entries,
            "truncated_count": truncated_count,
            "counts_by_feature": counts_by_feature,
        }

    await db.commit()

    await logger.ainfo(
        "admin.feature_override.expired_swept",
        actor_user_id=user.id,
        actor_email=user.email,
        deleted_count=deleted_count,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.feature_override.expired_swept",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=None,
        target_org_name=None,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail=detail,
    )
    return {"deleted_count": deleted_count}


@router.put(
    "/{org_id}/feature-overrides/{feature_key}",
    response_model=OrgFeatureOverrideResponse,
)
async def set_feature_override(
    org_id: int,
    feature_key: str,
    body: FeatureOverrideUpsert,
    request: Request,
    user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    try:
        _validate_feature_key(feature_key)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    org_row = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    target_org_name = org_row.name

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
                existing.set_at = utcnow_naive()
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

    await logger.ainfo(
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
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.org.feature.set",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={
            "feature_key": feature_key,
            "old_value": old_value,
            "new_value": body.value,
            "old_expires_at": old_expires_at.isoformat() if old_expires_at else None,
            "new_expires_at": body.expires_at.isoformat() if body.expires_at else None,
            "note_present": body.note is not None,
        },
    )

    return await _override_to_response(row, db)


@router.delete(
    "/{org_id}/feature-overrides/{feature_key}",
    status_code=204,
)
async def revoke_feature_override(
    org_id: int,
    feature_key: str,
    request: Request,
    user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    # 400 if key isn't in the catalog (matches PUT translation).
    try:
        _validate_feature_key(feature_key)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    org_row = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    target_org_name = org_row.name

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

    await logger.ainfo(
        "admin.org.feature.revoked",
        target_org_id=org_id,
        feature_key=feature_key,
        old_value=old_value,
        actor_user_id=user.id,
        actor_email=user.email,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="admin.org.feature.revoked",
        actor_user_id=user.id,
        actor_email=user.email,
        target_org_id=org_id,
        target_org_name=target_org_name,
        request_id=_request_id(),
        ip_address=get_client_ip(request),
        outcome="success",
        detail={"feature_key": feature_key, "old_value": old_value},
    )
    return Response(status_code=204)


# ── Feature state composite (T16) ────────────────────────────────────────


@router.get(
    "/{org_id}/feature-state",
    response_model=FeatureStateResponse,
)
async def get_feature_state(
    org_id: int,
    user: User = Depends(require_permission("orgs.view")),
    db: AsyncSession = Depends(get_db),
):
    # 404 explicit on missing target org. Resolver fail-closed (all-False)
    # is for product feature gates against the auth user's own org;
    # admin reads have a different contract.
    org = await db.scalar(select(Organization).where(Organization.id == org_id))
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    plan_features = await feature_service._fetch_plan_features(db, org_id)

    plan_row = await db.execute(
        select(Plan.id, Plan.name, Plan.slug)
        .join(Subscription, Subscription.plan_id == Plan.id)
        .where(Subscription.org_id == org_id)
    )
    plan_data = plan_row.first()
    plan_summary = (
        {"id": plan_data.id, "name": plan_data.name, "slug": plan_data.slug}
        if plan_data else None
    )

    # All overrides (active + expired) joined to setter email.
    rows = await db.execute(
        select(OrgFeatureOverride, User.email)
        .outerjoin(User, User.id == OrgFeatureOverride.set_by)
        .where(OrgFeatureOverride.org_id == org_id)
    )
    now = utcnow_naive()
    overrides_by_key: dict[str, dict] = {}
    for row, email in rows.all():
        if row.feature_key not in ALL_FEATURE_KEYS:
            continue  # defensive filter
        is_expired = row.expires_at is not None and row.expires_at <= now
        overrides_by_key[row.feature_key] = {
            "feature_key": row.feature_key,
            "value": row.value,
            "set_by": row.set_by,
            "set_by_email": email,
            "set_at": row.set_at.isoformat() if row.set_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "note": row.note,
            "is_expired": is_expired,
        }

    feature_rows = []
    for key in sorted(ALL_FEATURE_KEYS):
        plan_default = plan_features.get(key, False)
        ovr = overrides_by_key.get(key)
        effective = ovr["value"] if (ovr and not ovr["is_expired"]) else plan_default
        feature_rows.append({
            "key": key,
            "plan_default": plan_default,
            "effective": effective,
            "override": ovr,
        })

    return {"plan": plan_summary, "features": feature_rows}


# ── Org members (L4.4 — superadmin escape hatch) ─────────────────────────


def _audit_event_type_for_member_update(changes: list[str], after: dict) -> Optional[str]:
    """Pick the single event type for a member PATCH.

    Emits one event type per request; the UI sends one field at a
    time, so role and is_active changes never combine in a single
    PATCH today. If a future caller sends both in one body, the
    activation/deactivation flip takes precedence (it's the more
    operationally significant signal — a deactivated member can't
    use any role), and the role change is still captured in the
    ``before`` / ``after`` / ``changed_fields`` detail of the same
    audit row. If nothing actually changed the route returns 200
    without writing an audit row.
    """
    if "is_active" in changes:
        return (
            "admin.org.member.reactivated"
            if after["is_active"]
            else "admin.org.member.deactivated"
        )
    if "role" in changes:
        return "admin.org.member.role_changed"
    return None


async def _ensure_target_org(db: AsyncSession, org_id: int) -> Organization:
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return org


@router.get(
    "/{org_id}/members",
    response_model=list[AdminMemberResponse],
    dependencies=[Depends(require_permission("orgs.view"))],
)
async def list_org_members(
    org_id: int,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await admin_org_members_service.list_members(db, org_id=org_id)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )


@router.patch(
    "/{org_id}/members/{user_id}",
    response_model=AdminMemberResponse,
)
async def update_org_member(
    org_id: int,
    user_id: int,
    body: AdminMemberUpdateRequest,
    request: Request,
    current_user: User = Depends(require_permission("orgs.manage")),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    org = await _ensure_target_org(db, org_id)
    target_org_name = org.name

    role_arg: Optional[Role] = None
    if body.role is not None:
        try:
            role_arg = Role(body.role)
        except ValueError:
            # Pydantic Literal already constrains to the three roles,
            # but a defensive translation keeps the contract honest.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown role {body.role!r}",
            )

    try:
        target, before, after, changes = await admin_org_members_service.update_member(
            db,
            org_id=org_id,
            user_id=user_id,
            actor=current_user,
            role=role_arg,
            is_active=body.is_active,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found",
        )
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ConflictError as e:
        # The "platform superadmin" guard surfaces here as a
        # ConflictError; HTTP-wise it belongs at 403 because the
        # superadmin status is an authorization boundary, not a
        # transient conflict. Detect by message.
        msg = str(e)
        if "superadmin" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=msg
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=msg
        )

    event_type = _audit_event_type_for_member_update(changes, after)

    await db.commit()

    member_payload = {
        "id": target.id,
        "username": target.username,
        "email": target.email,
        "role": target.role.value,
        "is_active": target.is_active,
        "email_verified": target.email_verified,
        "is_superadmin": target.is_superadmin,
        "created_at": target.created_at.isoformat() if target.created_at else None,
    }

    if event_type is not None:
        detail: dict[str, object] = {
            "target_user_id": target.id,
            "target_username": target.username,
            "target_email": target.email,
            "before": before,
            "after": after,
            "changed_fields": changes,
        }
        await logger.ainfo(
            event_type,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=org_id,
            target_org_name=target_org_name,
            target_user_id=target.id,
            before=before,
            after=after,
            changed_fields=changes,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type=event_type,
            actor_user_id=current_user.id,
            actor_email=current_user.email,
            target_org_id=org_id,
            target_org_name=target_org_name,
            request_id=_request_id(),
            ip_address=get_client_ip(request),
            outcome="success",
            detail=detail,
        )

    return member_payload


# Note: the prior ``DELETE /api/v1/admin/orgs/{org_id}/members/{user_id}``
# endpoint emitted ``admin.org.member.removed`` audit rows but the
# underlying service merely soft-deactivated the user. To stop
# the UI from advertising a "Remove" affordance whose effect is a
# deactivate, both the endpoint and the service helper were removed
# on 2026-05-14. Callers wanting the same effect should PATCH
# ``is_active=False`` against the existing member endpoint, which
# already emits ``admin.org.member.deactivated`` and goes through
# the same last-owner / self-target / superadmin guards.
