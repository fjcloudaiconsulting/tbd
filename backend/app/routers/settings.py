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


@router.post("/billing-period/close")
async def close_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    close_date: str | None = None,
):
    _require_admin(current_user)
    import datetime
    cd = datetime.date.fromisoformat(close_date) if close_date else None
    new_period = await billing_service.close_period(db, current_user.org_id, cd)
    return {
        "id": new_period.id,
        "start_date": new_period.start_date.isoformat(),
        "end_date": None,
    }
