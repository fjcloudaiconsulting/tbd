from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models.settings import OrgSetting
from app.models.user import Role, User
from app.schemas.settings import OrgSettingResponse, OrgSettingUpdate

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
