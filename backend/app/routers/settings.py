import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.schemas.settings import BillingCycleUpdate, OrgSettingResponse, OrgSettingUpdate
from app.services import billing_service

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    result = await db.execute(
        select(Organization).where(Organization.id == current_user.org_id)
    )
    org = result.scalar_one()
    org.billing_cycle_day = body.billing_cycle_day

    # Recalculate the current open period to match the new cycle day
    current_period = await billing_service.get_current_period(db, current_user.org_id)
    if current_period.end_date is None:
        import datetime
        from sqlalchemy import update
        from app.models.budget import Budget

        old_start = current_period.start_date
        today = datetime.date.today()
        new_day = body.billing_cycle_day
        y, m, d = today.year, today.month, today.day
        if d >= new_day:
            new_start = datetime.date(y, m, new_day)
        else:
            prev = datetime.date(y, m, 1) - datetime.timedelta(days=1)
            new_start = datetime.date(prev.year, prev.month, new_day)
        current_period.start_date = new_start

        # Update budgets tied to the old period start date
        if old_start != new_start:
            await db.execute(
                update(Budget)
                .where(Budget.org_id == current_user.org_id, Budget.period_start == old_start)
                .values(period_start=new_start)
            )

    await db.commit()
    return {"billing_cycle_day": org.billing_cycle_day}


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


@router.post("/billing-period")
async def create_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    start_date: datetime.date = None,
    end_date: datetime.date | None = None,
):
    """Create a billing period with explicit dates (for seeding/migration)."""
    _require_admin(current_user)
    from app.models.billing import BillingPeriod
    period = BillingPeriod(org_id=current_user.org_id, start_date=start_date, end_date=end_date)
    db.add(period)
    await db.commit()
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    close_date: datetime.date | None = None,
):
    _require_admin(current_user)
    new_period = await billing_service.close_period(db, current_user.org_id, close_date)
    return {
        "id": new_period.id,
        "start_date": new_period.start_date.isoformat(),
        "end_date": None,
    }
