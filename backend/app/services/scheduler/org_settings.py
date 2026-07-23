"""Typed per-org scheduler settings, stored under the ``scheduler.`` OrgSetting
namespace. Kept out of the generic user-facing settings writer so the RESERVED
``feature.`` namespace guard is untouched.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import OrgSetting
from app.services.settings_service import get_org_setting

AUTOMATE_RECURRING_KEY = "scheduler.automate_recurring_generation"
AUTOMATE_BILLING_KEY = "scheduler.automate_billing_close"
REMINDER_LEAD_DAYS_KEY = "scheduler.billing_close_reminder_lead_days"
AUTOMATE_CC_STATEMENT_KEY = "scheduler.automate_cc_statement_alerts"
CC_STATEMENT_REMINDER_LEAD_DAYS_KEY = "scheduler.cc_statement_reminder_lead_days"

_BOOL_DEFAULTS = {
    AUTOMATE_RECURRING_KEY: "true",
    AUTOMATE_BILLING_KEY: "true",
    AUTOMATE_CC_STATEMENT_KEY: "true",
}
_REMINDER_DEFAULT = 3
_REMINDER_MIN, _REMINDER_MAX = 0, 31
_CC_STATEMENT_LEAD_DEFAULT = 2
_CC_MIN, _CC_MAX = 0, 31


async def get_bool(db: AsyncSession, org_id: int, key: str) -> bool:
    raw = await get_org_setting(db, org_id, key, _BOOL_DEFAULTS.get(key, "false"))
    return str(raw).strip().lower() == "true"


async def get_reminder_lead_days(db: AsyncSession, org_id: int) -> int:
    raw = await get_org_setting(db, org_id, REMINDER_LEAD_DAYS_KEY, str(_REMINDER_DEFAULT))
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return _REMINDER_DEFAULT
    return max(_REMINDER_MIN, min(_REMINDER_MAX, val))


async def get_cc_statement_lead_days(db: AsyncSession, org_id: int) -> int:
    raw = await get_org_setting(
        db, org_id, CC_STATEMENT_REMINDER_LEAD_DAYS_KEY, str(_CC_STATEMENT_LEAD_DEFAULT)
    )
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return _CC_STATEMENT_LEAD_DEFAULT
    return max(_CC_MIN, min(_CC_MAX, val))


async def set_value(db: AsyncSession, org_id: int, key: str, value: str) -> None:
    """Upsert a single scheduler setting. Caller is responsible for commit."""
    row = (
        await db.execute(
            select(OrgSetting).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
    else:
        row.value = value


async def get_all(db: AsyncSession, org_id: int) -> dict:
    return {
        "automate_recurring_generation": await get_bool(db, org_id, AUTOMATE_RECURRING_KEY),
        "automate_billing_close": await get_bool(db, org_id, AUTOMATE_BILLING_KEY),
        "billing_close_reminder_lead_days": await get_reminder_lead_days(db, org_id),
        "automate_cc_statement_alerts": await get_bool(db, org_id, AUTOMATE_CC_STATEMENT_KEY),
        "cc_statement_reminder_lead_days": await get_cc_statement_lead_days(db, org_id),
    }
