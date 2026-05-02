"""L4.11 — feature entitlement resolver.

Pure service layer. No FastAPI dependencies. The resolver order is
defaults → plan.features → active org override. Override row presence
(not row.value truthiness) is what wins, so a row with value=False
correctly denies an otherwise plan-granted feature.
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.feature_catalog import ALL_FEATURE_KEYS, PlanFeatures
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import Plan, Subscription


class UnknownFeatureKey(Exception):
    """Programmer error: gate-site key not in the catalog.

    Deliberately does NOT subclass app.services.exceptions.ValidationError
    so it surfaces as HTTP 500, not 400 — bad input from app code is an
    operational alert, not a user-facing validation error.
    """

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"Unknown feature key: {key!r}")


async def get_features(db: AsyncSession, org_id: int) -> dict[str, bool]:
    """Return the effective feature map for an org.

    Resolution order: defaults (False) → plan.features → active override.
    Fail-closed if the org has no subscription (returns all-False).
    """
    plan_features = await _fetch_plan_features(db, org_id)
    overrides = await _fetch_active_overrides(db, org_id)

    merged = {key: False for key in ALL_FEATURE_KEYS}
    merged.update(plan_features)
    merged.update(overrides)
    return merged


async def has_feature(db: AsyncSession, org_id: int, key: str) -> bool:
    if key not in ALL_FEATURE_KEYS:
        raise UnknownFeatureKey(key)
    features = await get_features(db, org_id)
    return features[key]


async def _fetch_plan_features(db: AsyncSession, org_id: int) -> dict[str, bool]:
    row = await db.execute(
        select(Plan.features)
        .join(Subscription, Subscription.plan_id == Plan.id)
        .where(Subscription.org_id == org_id)
    )
    raw = row.scalar_one_or_none() or {}
    # Read-side validation: bad DB data must fail loudly.
    return PlanFeatures.model_validate(raw).model_dump(by_alias=True)


async def _fetch_active_overrides(db: AsyncSession, org_id: int) -> dict[str, bool]:
    rows = await db.execute(
        select(OrgFeatureOverride.feature_key, OrgFeatureOverride.value)
        .where(OrgFeatureOverride.org_id == org_id)
        .where(or_(
            OrgFeatureOverride.expires_at.is_(None),
            OrgFeatureOverride.expires_at > func.now(),
        ))
    )
    # Defensive filter — a stale row predating a catalog key removal must not leak.
    return {r.feature_key: r.value for r in rows.all() if r.feature_key in ALL_FEATURE_KEYS}
