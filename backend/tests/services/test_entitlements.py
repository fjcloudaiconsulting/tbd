"""TBD-585: ``feature_service.get_entitlements``, the one resolver.

Fences F-E1 (merge semantics) and F-Q11 (resolver half: a raw plan JSON is
read through the catalog model). Every fence names the wrong implementation it
kills.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.feature_catalog import ALL_FEATURE_KEYS, ALL_METER_KEYS
from app.models import Base
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.user import Organization
from app.services import feature_service
from app.services.feature_service import UsageLimit, get_entitlements

NOW = datetime(2026, 9, 30, 12, 0, 0)
CANONICAL = {
    "assistant.turns": UsageLimit("month", None),
    "mcp.calls": UsageLimit("month", None),
    "platform_ai.tokens": UsageLimit("month", 0),
    "platform_ai.cents": UsageLimit("month", 0),
}


@pytest_asyncio.fixture
async def factory(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


async def _org(f, *, features=None, usage_limits=None, subscribe=True) -> int:
    async with f() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        if subscribe:
            plan = Plan(slug=f"p{org.id}", name="P", features=features or {})
            if usage_limits is not None:
                plan.usage_limits = usage_limits
            db.add(plan)
            await db.flush()
            db.add(Subscription(org_id=org.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE))
        await db.commit()
        return org.id


async def _add(f, *rows):
    async with f() as db:
        db.add_all(rows)
        await db.commit()


async def _ent(f, org_id, now=NOW):
    async with f() as db:
        return await get_entitlements(db, org_id, now=now)


async def test_fe1_feature_override_presence_wins_both_ways(factory):
    """FENCE F-E1. Wrong implementations: truthiness not presence
    (``if row.value``), and ``override or plan`` (a False row falls through to
    the plan's True)."""
    org = await _org(factory, features={"ai.budget": True})
    await _add(
        factory,
        OrgFeatureOverride(org_id=org, feature_key="ai.budget", value=False),
        OrgFeatureOverride(org_id=org, feature_key="ai.forecast", value=True),
    )
    ent = await _ent(factory, org)
    assert ent.features["ai.budget"] is False
    assert ent.features["ai.forecast"] is True
    assert ent.plan_features["ai.budget"] is True and ent.plan_features["ai.forecast"] is False
    assert ent.overridden == {"ai.budget", "ai.forecast"}


async def test_fe1_limit_override_replaces_wholesale(factory):
    """FENCE F-E1. Wrong implementations: max/min merge (0 over 100 kept at
    100, None over 5 kept at 5), None read as "no override", and a per-field
    merge that keeps the plan's period."""
    org = await _org(factory, usage_limits={
        "mcp.calls": {"period": "month", "limit": 100},
        "assistant.turns": {"period": "month", "limit": 5},
        "platform_ai.tokens": {"period": "month", "limit": 100},
    })
    await _add(
        factory,
        OrgLimitOverride(org_id=org, meter="mcp.calls", period="month", limit_value=0),
        OrgLimitOverride(org_id=org, meter="assistant.turns", period="month", limit_value=None),
        OrgLimitOverride(org_id=org, meter="platform_ai.tokens", period="day", limit_value=5),
    )
    ent = await _ent(factory, org)
    assert ent.limits["mcp.calls"] == UsageLimit("month", 0)
    assert ent.limits["assistant.turns"] == UsageLimit("month", None)
    assert ent.limits["platform_ai.tokens"] == UsageLimit("day", 5)
    assert ent.plan_limits["mcp.calls"] == UsageLimit("month", 100)
    assert ent.overridden == {"mcp.calls", "assistant.turns", "platform_ai.tokens"}


async def test_fe1_expiry_boundary_on_the_given_clock(factory):
    """FENCE F-E1. Wrong implementations: an expired override honoured, and
    ``>=`` for ``>`` (``now == expires_at`` still active)."""
    org = await _org(factory)
    await _add(
        factory,
        OrgFeatureOverride(org_id=org, feature_key="ai.agent", value=True, expires_at=NOW),
        OrgLimitOverride(org_id=org, meter="mcp.calls", period="day", limit_value=3,
                         expires_at=NOW),
    )
    at = await _ent(factory, org, NOW)
    assert at.features["ai.agent"] is False and at.limits["mcp.calls"] == CANONICAL["mcp.calls"]
    assert at.overridden == frozenset()
    before = await _ent(factory, org, NOW - timedelta(microseconds=1))
    assert before.features["ai.agent"] is True
    assert before.limits["mcp.calls"] == UsageLimit("day", 3)


async def test_fe1_no_subscription_no_override_is_all_off_and_canonical(factory):
    """FENCE F-E1. Wrong implementation: a missing plan read as "everything
    on" or as unlimited platform spend."""
    org = await _org(factory, subscribe=False)
    ent = await _ent(factory, org)
    assert ent.has_plan is False
    assert ent.features == {k: False for k in ALL_FEATURE_KEYS}
    assert ent.limits == CANONICAL and ent.plan_limits == CANONICAL


async def test_fe1_no_subscription_overrides_still_apply(factory):
    org = await _org(factory, subscribe=False)
    await _add(factory, OrgFeatureOverride(org_id=org, feature_key="ai.agent", value=True))
    assert (await _ent(factory, org)).features["ai.agent"] is True


async def test_fe1_tampered_null_platform_override_raises(factory):
    """FENCE F-E1. Wrong implementation: merged limits not re-validated, so a
    NULL ``platform_ai.*`` row written around the API is unlimited platform
    spend."""
    org = await _org(factory)
    await _add(factory, OrgLimitOverride(org_id=org, meter="platform_ai.cents", period="month",
                                         limit_value=None))
    with pytest.raises(PydanticValidationError):
        await _ent(factory, org)


async def test_fe1_stale_override_keys_are_filtered(factory):
    org = await _org(factory)
    await _add(
        factory,
        OrgFeatureOverride(org_id=org, feature_key="ai.retired", value=True),
        OrgLimitOverride(org_id=org, meter="retired.meter", period="day", limit_value=1),
    )
    ent = await _ent(factory, org)
    assert set(ent.features) == ALL_FEATURE_KEYS and set(ent.limits) == ALL_METER_KEYS


@pytest.mark.parametrize("stored", [{}, {"assistant.turns": {"period": "day", "limit": 2}}])
async def test_fq11_raw_plan_json_reads_with_defaults(factory, stored):
    """FENCE F-Q11 (resolver half). Wrong implementation: reading the raw JSON
    (``plan.usage_limits["mcp.calls"]`` raises KeyError on ``{}`` or a plan
    that predates a meter)."""
    org = await _org(factory, usage_limits=stored)
    ent = await _ent(factory, org)
    assert ent.limits["mcp.calls"] == CANONICAL["mcp.calls"]
    assert set(ent.limits) == ALL_METER_KEYS


async def test_get_features_goes_through_the_patchable_resolver(factory, monkeypatch):
    """``get_features``/``has_feature`` look ``get_entitlements`` up through the
    module global, so one patch reaches every consumer (F-E2's runtime half
    builds on this)."""
    org = await _org(factory)
    real = feature_service.get_entitlements

    async def _all_on(db, org_id, *, now=None):
        ent = await real(db, org_id, now=now)
        return type(ent)(**{**ent.__dict__, "features": {k: True for k in ALL_FEATURE_KEYS}})

    monkeypatch.setattr(feature_service, "get_entitlements", _all_on)
    async with factory() as db:
        assert await feature_service.has_feature(db, org, "plans") is True
