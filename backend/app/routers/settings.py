import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models.billing import BillingPeriod
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.rate_limit import get_client_ip
from app.schemas.settings import (
    BillingCycleUpdate,
    BillingPeriodCreate,
    ManualBalanceAdjustmentResponse,
    ManualBalanceAdjustmentToggle,
    OrgSettingResponse,
    OrgSettingUpdate,
)
from app.services import audit_service, billing_service
from app.services.exceptions import ConflictError, ValidationError
from app.services.settings_service import (
    FORECAST_GRANULARITY_VALUES,
    FORECAST_INPUT_GRANULARITY_KEY,
)

logger = structlog.stdlib.get_logger()

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

# The "feature." prefix is exclusively managed by superadmin endpoints in
# admin_features.py, which layer per-org OrgSetting overrides at the highest
# priority in the three-level feature gate (feature_gate.py: per-org >
# SystemSetting global > env-floor).  Allowing the generic PUT/DELETE here
# would let any OWNER/ADMIN bypass a globally-disabled feature with no audit
# trail.  Block the entire namespace from this writer.
RESERVED_SETTINGS_PREFIX = "feature."


def _request_id() -> str | None:
    """Pull the per-request id bound by RequestContextMiddleware."""
    return structlog.contextvars.get_contextvars().get("request_id")


def _require_admin(user: User) -> None:
    if user.role not in (Role.OWNER, Role.ADMIN) and not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


@router.get("", response_model=list[OrgSettingResponse])
async def list_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    result = await db.execute(
        select(OrgSetting)
        .where(OrgSetting.org_id == current_user.org_id)
        .order_by(OrgSetting.key)
    )
    return [
        OrgSettingResponse(key=s.key, value=s.value) for s in result.scalars().all()
    ]


@router.put("", response_model=OrgSettingResponse)
async def upsert_setting(
    body: OrgSettingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    if body.key.startswith(RESERVED_SETTINGS_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The 'feature.' settings namespace is managed by platform administrators",
        )

    # Per-key bounds validation. Other org settings have no bounds
    # contract today; only the session-lifetime key actually drives
    # the session TTL, so an out-of-bounds write here would log
    # users out instantly or hand them a year-long session.
    if body.key == "session_lifetime_days":
        try:
            days = int(body.value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_lifetime_days must be an integer (days)",
            )
        if not (1 <= days <= 365):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_lifetime_days must be between 1 and 365",
            )

    # Forecast build granularity is a closed enum (master|subcategory). The
    # service defends by falling back to master on garbage, but rejecting a
    # bad write here avoids a silently-ignored setting that confuses admins.
    if body.key == FORECAST_INPUT_GRANULARITY_KEY:
        if body.value not in FORECAST_GRANULARITY_VALUES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "forecast_input_granularity must be one of: "
                    f"{', '.join(FORECAST_GRANULARITY_VALUES)}"
                ),
            )

    result = await db.execute(
        select(OrgSetting).where(
            OrgSetting.org_id == current_user.org_id,
            OrgSetting.key == body.key,
        )
    )
    setting = result.scalar_one_or_none()

    if setting:
        setting.value = body.value
    else:
        setting = OrgSetting(
            org_id=current_user.org_id, key=body.key, value=body.value
        )
        db.add(setting)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Concurrent insert won the race — retry as update
        result = await db.execute(
            select(OrgSetting).where(
                OrgSetting.org_id == current_user.org_id,
                OrgSetting.key == body.key,
            )
        )
        setting = result.scalar_one()
        setting.value = body.value
        await db.commit()

    await db.refresh(setting)
    return OrgSettingResponse(key=setting.key, value=setting.value)


@router.delete("/{key}", status_code=204)
async def delete_setting(
    key: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)

    if key.startswith(RESERVED_SETTINGS_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The 'feature.' settings namespace is managed by platform administrators",
        )

    result = await db.execute(
        select(OrgSetting).where(
            OrgSetting.org_id == current_user.org_id,
            OrgSetting.key == key,
        )
    )
    setting = result.scalar_one_or_none()
    if setting is None:
        raise HTTPException(status_code=404, detail="Setting not found")

    await db.delete(setting)
    await db.commit()


@router.get("/billing-cycle")
async def get_billing_cycle(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    org = result.scalar_one()
    return {"billing_cycle_day": org.billing_cycle_day}


@router.put("/billing-cycle")
async def update_billing_cycle(
    body: BillingCycleUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Re-root the open period on a new cycle day and move its budgets with it.

    This closes nothing and creates nothing: the open period's ``start_date``
    moves in place and every budget anchored to the old start follows.

    The budget move lives in ``billing_service.reanchor_period_dependents``
    so TBD-235's boundary editor does not become a second implementation of
    the same thing. The ``if old_start != new_start:`` guard below is kept —
    only the inline ``UPDATE Budget`` it used to wrap is gone.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db so a rollback path
    # can't expire `current_user` and break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    result = await db.execute(
        select(Organization).where(Organization.id == actor_org_id)
    )
    org = result.scalar_one()
    org_name = org.name
    old_day = org.billing_cycle_day
    new_day = body.billing_cycle_day
    org.billing_cycle_day = new_day

    # Recalculate the current open period to match the new cycle day
    current_period = await billing_service.get_current_period(db, actor_org_id)
    period_id = current_period.id
    old_start = current_period.start_date
    new_start = old_start
    reanchored = 0

    async def _audit(outcome: str, **extra) -> None:
        """Structlog breadcrumb + audit row for this endpoint.

        ``record_audit_event`` swallows every exception by design
        (audit_service.py) and names "the structlog event the caller already
        emitted" as its fallback record, so the log line has to be emitted
        first — otherwise a transient audit-session failure leaves no trace
        at all that a period boundary and its budgets moved.
        """
        payload = {
            "old_day": old_day,
            "new_day": new_day,
            "period_id": period_id,
            "old_start": old_start.isoformat(),
            "new_start": new_start.isoformat(),
            **extra,
        }
        await logger.ainfo(
            "org.billing_cycle_day.updated",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            outcome=outcome,
            **payload,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.billing_cycle_day.updated",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=org_name,
            request_id=req_id,
            ip_address=ip,
            outcome=outcome,
            detail=payload,
        )

    if current_period.end_date is None:
        today = datetime.date.today()
        y, m, d = today.year, today.month, today.day
        if d >= new_day:
            new_start = datetime.date(y, m, new_day)
        else:
            prev = datetime.date(y, m, 1) - datetime.timedelta(days=1)
            new_start = datetime.date(prev.year, prev.month, new_day)

        if old_start != new_start:
            # uq_billing_period_org_start pre-flight. Excludes this period so
            # the identity case can never collide with itself.
            clash = await db.scalar(
                select(BillingPeriod.id).where(
                    BillingPeriod.org_id == actor_org_id,
                    BillingPeriod.start_date == new_start,
                    BillingPeriod.id != period_id,
                )
            )
            if clash is not None:
                await db.rollback()
                await _audit(
                    "failure",
                    reason="billing_period_exists",
                    conflicting_period_id=clash,
                )
                raise ConflictError(
                    f"A billing period already starts on {new_start.isoformat()}",
                    code="billing_period_exists",
                )
            current_period.start_date = new_start

            # Flush the period write HERE, where an IntegrityError is still
            # attributable to it. Left pending, autoflush would fire it from
            # the first `db.execute` inside `reanchor_period_dependents` —
            # i.e. a uq_billing_period_org_start violation raised outside
            # this handler (unhandled 500, the exact failure class this
            # slice removes) or, once TBD-235 makes that helper run twice,
            # swallowed by its own `except IntegrityError` and reported as
            # `budget_period_conflict` with a message about budgets that
            # never moved. The pre-flight above is TOCTOU: a concurrent PUT
            # or BillingCloseJob (900s tick, `automate_billing_close` on by
            # default) can land a period on `new_start` in between.
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                await _audit(
                    "failure", reason="billing_period_exists", race=True
                )
                raise ConflictError(
                    f"A billing period already starts on {new_start.isoformat()}",
                    code="billing_period_exists",
                )

        # `new_end` is the OPEN period's end_date, which is None by
        # construction. Do NOT substitute a projected end here: it would
        # write a non-null snapshot onto every open-period budget.
        try:
            reanchored = await billing_service.reanchor_period_dependents(
                db,
                org_id=actor_org_id,
                old_start=old_start,
                new_start=new_start,
                new_end=current_period.end_date,
            )
        except ConflictError as exc:
            await db.rollback()
            await _audit(
                "failure",
                reason=exc.code or "conflict",
                message=exc.detail,
            )
            raise

    await db.commit()

    await _audit("success", budgets_reanchored=reanchored)

    return {"billing_cycle_day": new_day}


@router.get("/billing-period")
async def get_current_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    period = await billing_service.get_current_period(db, current_user.org_id)
    return {
        "id": period.id,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat() if period.end_date else None,
    }


@router.get("/billing-periods")
async def list_periods(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    periods = await billing_service.list_periods(db, current_user.org_id)
    return [
        {
            "id": p.id,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat() if p.end_date else None,
        }
        for p in periods
    ]


@router.post("/billing-period", status_code=200)
async def create_period(
    body: BillingPeriodCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a billing period with explicit dates (for seeding/migration).

    ``status_code`` stays 200 — the pre-existing contract of an endpoint whose
    only callers live in ``seed.py``; there is nothing to gain by churning it
    to 201. ``seed.py`` no longer branches on the status code itself: it hands
    the response to ``seed.billing_period_outcome``, which absorbs the 409
    below (the seed dataset is re-runnable and its start dates are
    deterministic) and raises on everything else.
    """
    _require_admin(current_user)

    existing = await db.scalar(
        select(BillingPeriod.id).where(
            BillingPeriod.org_id == current_user.org_id,
            BillingPeriod.start_date == body.start_date,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"A billing period already starts on {body.start_date.isoformat()}",
            code="billing_period_exists",
        )

    period = BillingPeriod(
        org_id=current_user.org_id,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    db.add(period)
    try:
        await db.commit()
    except IntegrityError:
        # TOCTOU backstop for uq_billing_period_org_start.
        await db.rollback()
        raise ConflictError(
            f"A billing period already starts on {body.start_date.isoformat()}",
            code="billing_period_exists",
        )
    await db.refresh(period)
    return {
        "id": period.id,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat() if period.end_date else None,
    }


@router.post("/billing-periods/ensure-future")
async def ensure_future_periods(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    count: int = 3,
):
    """Create stub periods for upcoming months so the user can plan ahead."""
    _require_admin(current_user)
    count = min(max(count, 1), 6)  # Cap between 1 and 6 months
    created = await billing_service.ensure_future_periods(db, current_user.org_id, count=count)
    return [
        {
            "id": p.id,
            "start_date": p.start_date.isoformat(),
            "end_date": p.end_date.isoformat() if p.end_date else None,
        }
        for p in created
    ]


@router.post("/billing-period/close")
async def close_period(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    close_date: datetime.date | None = None,
):
    """Close the open period and open the next one.

    ``close_period`` commits internally and returns only the NEW period, so
    the audit detail is assembled around it: the closing period is snapshotted
    via ``get_current_period`` BEFORE the call, and the resolved close date is
    derived as ``new_period.start_date - 1 day``. Re-implementing the service's
    "yesterday" default here would drift from it.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db so a rollback path
    # can't expire `current_user` and break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    org_name = await db.scalar(
        select(Organization.name).where(Organization.id == actor_org_id)
    )

    closing = await billing_service.get_current_period(db, actor_org_id)
    closed_period_id = closing.id
    closed_period_start = closing.start_date

    async def _audit(outcome: str, **extra) -> None:
        """Structlog breadcrumb + audit row for this endpoint.

        ``record_audit_event`` swallows every exception by design
        (audit_service.py) and names "the structlog event the caller already
        emitted" as its fallback record, so the log line goes first.
        """
        payload = {
            "closed_period_id": closed_period_id,
            "closed_period_start": closed_period_start.isoformat(),
            **extra,
        }
        await logger.ainfo(
            "org.billing_period.closed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            outcome=outcome,
            **payload,
        )
        await audit_service.record_audit_event(
            session_factory,
            event_type="org.billing_period.closed",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_org_id=actor_org_id,
            target_org_name=org_name,
            request_id=req_id,
            ip_address=ip,
            outcome=outcome,
            detail=payload,
        )

    try:
        new_period = await billing_service.close_period(db, actor_org_id, close_date)
    except Exception as exc:  # noqa: BLE001 — nothing may close unaudited.
        # ValidationError (close date before the period start) is not the only
        # way out of `close_period`: it also raises RuntimeError when the row
        # vanishes after its own IntegrityError retry, and IntegrityError can
        # escape its second commit. Catching only ValidationError left both as
        # unaudited 500s. `org_data.py`'s reset path is the house reference:
        # catch broadly, audit, re-raise untouched.
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001 — best effort; the audit matters more
            pass
        await _audit(
            "failure",
            close_date=close_date.isoformat() if close_date else None,
            reason="validation" if isinstance(exc, ValidationError) else "error",
            message=getattr(exc, "detail", None) or str(exc),
            error_type=type(exc).__name__,
        )
        raise

    resolved_close_date = new_period.start_date - datetime.timedelta(days=1)

    await _audit(
        "success",
        close_date=resolved_close_date.isoformat(),
        new_period_id=new_period.id,
        new_period_start=new_period.start_date.isoformat(),
    )

    return {
        "id": new_period.id,
        "start_date": new_period.start_date.isoformat(),
        "end_date": None,
    }


# ── Track E: manual balance adjustment toggle ─────────────────────────────


@router.get(
    "/manual-balance-adjustment",
    response_model=ManualBalanceAdjustmentResponse,
)
async def get_manual_balance_adjustment(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current org's manual-balance-adjustment toggle.
    Available to any org member (the frontend uses it to render or hide
    the "Adjust balance" button on each account card).
    """
    org = await db.scalar(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return ManualBalanceAdjustmentResponse(
        enabled=org.allow_manual_balance_adjustment
    )


@router.put(
    "/manual-balance-adjustment",
    response_model=ManualBalanceAdjustmentResponse,
)
async def update_manual_balance_adjustment(
    body: ManualBalanceAdjustmentToggle,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Track E: admin-only toggle for manual balance adjustment.

    Writes an audit row even on no-op (old == new) so a paranoid admin
    can confirm "yes, I checked the toggle and it's still off". The
    audit row commits in an independent session via ``record_audit_event``
    AFTER the business commit so the admin's UI doesn't hang on audit
    DB hiccups, and an audit failure can never roll back a successful
    toggle write.
    """
    _require_admin(current_user)

    # Snapshot actor identity before any await on db so a rollback path
    # can't expire `current_user` and break the audit row.
    actor_user_id = current_user.id
    actor_email = current_user.email
    actor_org_id = current_user.org_id
    req_id = _request_id()
    ip = get_client_ip(request)

    org = await db.scalar(
        select(Organization).where(Organization.id == actor_org_id)
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    old_value = bool(org.allow_manual_balance_adjustment)
    new_value = bool(body.enabled)
    org.allow_manual_balance_adjustment = new_value
    org_name = org.name
    await db.commit()

    await logger.ainfo(
        "org.config.allow_manual_balance_adjustment.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        old=old_value,
        new=new_value,
    )
    await audit_service.record_audit_event(
        session_factory,
        event_type="org.config.allow_manual_balance_adjustment.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=actor_org_id,
        target_org_name=org_name,
        request_id=req_id,
        ip_address=ip,
        outcome="success",
        detail={"old": old_value, "new": new_value},
    )

    return ManualBalanceAdjustmentResponse(enabled=new_value)
