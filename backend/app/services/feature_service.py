"""L4.11 / TBD-585 — the entitlement resolver (features and usage limits).

Pure service layer. No FastAPI dependencies. The resolver order is
defaults → plan → active org override. Override row presence
(not row.value truthiness) is what wins, so a row with value=False
correctly denies an otherwise plan-granted feature.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.auth.feature_catalog import (
    ALL_FEATURE_KEYS,
    ALL_METER_KEYS,
    PlanFeatures,
    PlanUsageLimits,
)
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
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


@dataclass(frozen=True)
class UsageLimit:
    period: str
    limit: int | None  # None = unlimited, 0 = none


@dataclass(frozen=True)
class Entitlements:
    """What an org may use, resolved once. ``plan_*`` are the plan's values
    before overrides (catalog defaults when there is no subscription);
    ``overridden`` names every feature key and meter an ACTIVE override set."""

    features: dict[str, bool]
    limits: dict[str, UsageLimit]
    plan_features: dict[str, bool]
    plan_limits: dict[str, UsageLimit]
    has_plan: bool
    overridden: frozenset[str]


def _limits(model: PlanUsageLimits) -> dict[str, UsageLimit]:
    return {m: UsageLimit(**v) for m, v in model.model_dump(by_alias=True).items()}


async def get_entitlements(
    db: AsyncSession, org_id: int, *, now: datetime | None = None
) -> Entitlements:
    """The ONE entitlement resolver: defaults -> plan -> active overrides.

    Override row PRESENCE wins (a False feature row denies a plan grant, a
    NULL limit row makes the meter unlimited, a limit row replaces the meter's
    ``{period, limit}`` wholesale). An override is active while
    ``expires_at IS NULL OR expires_at > now`` on the app clock. No
    subscription: catalog defaults, overrides still apply. Bad stored data
    (an unknown plan key, a null platform limit) raises.
    """
    now = now or utcnow_naive()
    plan = (
        await db.execute(
            select(Plan.features, Plan.usage_limits)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.org_id == org_id)
        )
    ).first()
    plan_features = PlanFeatures.model_validate(
        (plan.features if plan else None) or {}
    ).model_dump(by_alias=True)
    plan_limits = PlanUsageLimits.model_validate((plan.usage_limits if plan else None) or {})

    feature_rows = (
        await db.execute(
            select(OrgFeatureOverride.feature_key, OrgFeatureOverride.value)
            .where(OrgFeatureOverride.org_id == org_id)
            .where(or_(OrgFeatureOverride.expires_at.is_(None), OrgFeatureOverride.expires_at > now))
        )
    ).all()
    limit_rows = (
        await db.execute(
            select(OrgLimitOverride.meter, OrgLimitOverride.period, OrgLimitOverride.limit_value)
            .where(OrgLimitOverride.org_id == org_id)
            .where(or_(OrgLimitOverride.expires_at.is_(None), OrgLimitOverride.expires_at > now))
        )
    ).all()
    # Defensive filter: a stale row predating a catalog removal must not leak.
    feature_ovr = {r.feature_key: r.value for r in feature_rows if r.feature_key in ALL_FEATURE_KEYS}
    limit_ovr = {
        r.meter: {"period": r.period, "limit": r.limit_value}
        for r in limit_rows if r.meter in ALL_METER_KEYS
    }
    # Validated once more: a tampered NULL platform override fails loudly.
    merged_limits = PlanUsageLimits.model_validate(
        {**plan_limits.model_dump(by_alias=True), **limit_ovr}
    )
    return Entitlements(
        features={**plan_features, **feature_ovr},
        limits=_limits(merged_limits),
        plan_features=plan_features,
        plan_limits=_limits(plan_limits),
        has_plan=plan is not None,
        overridden=frozenset(feature_ovr) | frozenset(limit_ovr),
    )


async def get_features(db: AsyncSession, org_id: int) -> dict[str, bool]:
    """The effective feature map for an org (see :func:`get_entitlements`).

    Looked up through the module global so a patch of ``get_entitlements``
    reaches every consumer.
    """
    return (await get_entitlements(db, org_id)).features


async def has_feature(db: AsyncSession, org_id: int, key: str) -> bool:
    if key not in ALL_FEATURE_KEYS:
        raise UnknownFeatureKey(key)
    features = await get_features(db, org_id)
    return features[key]
