"""Org-settings accessors.

``org_settings`` is a key/value store (``backend/app/models/settings.py``).
This module centralizes typed reads so feature code doesn't re-inline the
``select(OrgSetting.value).where(...)`` pattern (mirrors the inline read at
``import_service.py:436`` for ``share_merchant_data``).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import OrgSetting

# ── Forecast build-granularity setting ─────────────────────────────────────
# Per-org preference controlling whether forecast plan items are built at the
# master-category level (default, preserves legacy behavior) or at the
# subcategory level (multiple subs summing into a master).
FORECAST_INPUT_GRANULARITY_KEY = "forecast_input_granularity"
FORECAST_GRANULARITY_MASTER = "master"
FORECAST_GRANULARITY_SUBCATEGORY = "subcategory"
FORECAST_GRANULARITY_VALUES = (
    FORECAST_GRANULARITY_MASTER,
    FORECAST_GRANULARITY_SUBCATEGORY,
)


async def get_org_setting(
    db: AsyncSession, org_id: int, key: str, default: str | None = None,
) -> str | None:
    """Return the value of an org setting, or ``default`` when unset."""
    value = (
        await db.execute(
            select(OrgSetting.value).where(
                OrgSetting.org_id == org_id,
                OrgSetting.key == key,
            )
        )
    ).scalar_one_or_none()
    return value if value is not None else default


async def get_forecast_input_granularity(db: AsyncSession, org_id: int) -> str:
    """Return the org's forecast build granularity (``master`` | ``subcategory``).

    Defaults to ``master`` (legacy behavior) when unset or set to an
    unrecognized value, so a stray/garbage write can never silently flip an
    org into subcategory mode.
    """
    value = await get_org_setting(
        db, org_id, FORECAST_INPUT_GRANULARITY_KEY, FORECAST_GRANULARITY_MASTER
    )
    if value not in FORECAST_GRANULARITY_VALUES:
        return FORECAST_GRANULARITY_MASTER
    return value
