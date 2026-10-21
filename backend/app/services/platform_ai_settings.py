"""Operator settings for platform-provided AI (TBD-586). Reads ``SystemSetting``
ONLY (never ``OrgSetting``: an org must not be able to flip any of these).
The model allowlist is ``platform_ai_models.enabled``, not a setting.

Every read fails closed: absent or malformed means off or 0."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.system_setting import SystemSetting
from app.services import feature_service, platform_ai

ENABLED = "platform_ai.enabled"
GLOBAL_MONTHLY_CENTS = "platform_ai.global_monthly_cents"


@dataclass(frozen=True)
class PlatformAISettings:
    enabled: bool = False
    global_monthly_cents: int = 0


async def load(db: AsyncSession) -> PlatformAISettings:
    rows = dict(
        (await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_((ENABLED, GLOBAL_MONTHLY_CENTS))
            )
        )).all()
    )
    cents = rows.get(GLOBAL_MONTHLY_CENTS) or ""
    return PlatformAISettings(
        enabled=rows.get(ENABLED) == "on",
        global_monthly_cents=int(cents) if cents.isascii() and cents.isdigit() else 0,
    )


def switched_on(conf: PlatformAISettings, platform_provider: str = "openrouter") -> bool:
    return bool(settings.ai_native_enabled and conf.enabled
                and platform_ai.platform_key_configured(platform_provider))


async def offered(db: AsyncSession, conf: PlatformAISettings, org_id: int) -> list[str]:
    """Model ids ``org_id`` can use now: env floor, the operator switch and the env key, then
    the offerable catalog rows its plan may run. Empty when none is paid (dispatch would have
    no default to run), e.g. a non-Pro org whose catalog is only frontier or free rows."""
    if not switched_on(conf):
        return []
    try:
        frontier = (await feature_service.get_entitlements(db, org_id)).frontier_models
    except (SQLAlchemyError, PydanticValidationError):
        await db.rollback()
        return []  # unreadable plan: offer nothing rather than 500 /options
    rows = await platform_ai.candidates(db, "function_call", frontier=frontier)
    return [r.openrouter_id for r in rows] if platform_ai.default_row(rows) else []
