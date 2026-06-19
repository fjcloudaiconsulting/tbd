"""Feature gate resolution service.

Three-level resolution (lowest → highest priority):

  1. Env-floor  — ``settings.feature_<name>`` (default False, operator-tunable)
  2. Global     — ``SystemSetting`` row with ``key="feature.<name>"`` and
                  ``value="on"`` / ``"off"``
  3. Per-org    — ``OrgSetting`` row with the same key scheme, scoped to
                  an ``org_id``

Unrecognised or absent values at any level fall through to the next.
Fail-closed: if env-floor is False and no DB rows exist, the feature is off.
"""
from __future__ import annotations

from enum import Enum

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user
from app.models.settings import OrgSetting
from app.models.system_setting import SystemSetting
from app.models.user import User


class Feature(str, Enum):
    REPORTS = "reports"
    PLANS = "plans"


_ENV_FLOOR = {
    Feature.REPORTS: lambda: app_settings.feature_reports_v2,
    Feature.PLANS: lambda: app_settings.feature_plans,
}


def feature_setting_key(feature: Feature) -> str:
    """Return the canonical DB key for a feature, e.g. ``"feature.reports"``."""
    return f"feature.{feature.value}"


def _parse_onoff(value: str | None) -> bool | None:
    """Parse ``"on"`` → True, ``"off"`` → False, anything else → None (fall through)."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "on":
        return True
    if v == "off":
        return False
    return None


async def resolve_feature(feature: Feature, org_id: int | None, db: AsyncSession) -> bool:
    """Return the effective on/off state for *feature* scoped to *org_id*.

    Resolution order: per-org OrgSetting → global SystemSetting → env-floor.

    When *org_id* is ``None`` (unauthenticated caller), the per-org lookup is
    skipped entirely and resolution falls through to global SystemSetting →
    env-floor only.
    """
    key = feature_setting_key(feature)

    # Level 3 — per-org override (skipped when caller is unauthenticated)
    if org_id is not None:
        org_val = await db.scalar(
            select(OrgSetting.value).where(OrgSetting.org_id == org_id, OrgSetting.key == key)
        )
        parsed = _parse_onoff(org_val)
        if parsed is not None:
            return parsed

    # Level 2 — global system setting
    global_val = await db.scalar(select(SystemSetting.value).where(SystemSetting.key == key))
    parsed = _parse_onoff(global_val)
    if parsed is not None:
        return parsed

    # Level 1 — env-floor fallback
    return bool(_ENV_FLOOR[feature]())


def require_feature(feature: Feature):
    """Return a FastAPI dependency that 404s when *feature* is off for the caller's org."""

    async def _dep(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        if not await resolve_feature(feature, current_user.org_id, db):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return _dep
