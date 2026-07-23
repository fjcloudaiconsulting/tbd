"""GET/PUT endpoint for per-org scheduler settings (automate recurring
generation, automate billing close, billing close reminder lead days,
automate CC statement alerts, CC statement reminder lead days).

Read is available to any authenticated org member; writes are gated
behind ``require_org_admin`` (OWNER/ADMIN), the same org-scoped admin
guard used by the manual billing-period close route in settings.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.org_permissions import require_org_admin
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.scheduler import SchedulerSettingsResponse, SchedulerSettingsUpdate
from app.services.scheduler import org_settings as so

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])


@router.get("/settings", response_model=SchedulerSettingsResponse)
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await so.get_all(db, current_user.org_id)


@router.put("/settings", response_model=SchedulerSettingsResponse)
async def put_settings(
    body: SchedulerSettingsUpdate,
    current_user: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    org_id = current_user.org_id
    if body.automate_recurring_generation is not None:
        await so.set_value(
            db,
            org_id,
            so.AUTOMATE_RECURRING_KEY,
            "true" if body.automate_recurring_generation else "false",
        )
    if body.automate_billing_close is not None:
        await so.set_value(
            db,
            org_id,
            so.AUTOMATE_BILLING_KEY,
            "true" if body.automate_billing_close else "false",
        )
    if body.billing_close_reminder_lead_days is not None:
        await so.set_value(
            db,
            org_id,
            so.REMINDER_LEAD_DAYS_KEY,
            str(body.billing_close_reminder_lead_days),
        )
    if body.automate_cc_statement_alerts is not None:
        await so.set_value(
            db,
            org_id,
            so.AUTOMATE_CC_STATEMENT_KEY,
            "true" if body.automate_cc_statement_alerts else "false",
        )
    if body.cc_statement_reminder_lead_days is not None:
        await so.set_value(
            db,
            org_id,
            so.CC_STATEMENT_REMINDER_LEAD_DAYS_KEY,
            str(body.cc_statement_reminder_lead_days),
        )
    await db.commit()
    return await so.get_all(db, org_id)
