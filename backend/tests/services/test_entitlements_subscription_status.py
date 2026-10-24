"""BE-05: entitlements follow the subscription status, not only the plan.

A PAST_DUE subscription resolves as the Free plan at once, whatever plan row
it still points at. A CANCELED one keeps its plan until its paid period has
ended (``current_period_end`` is the last day of access, as the cancel flow
promises), then resolves as Free; with no period end it is Free at once.
TRIALING and ACTIVE keep their plan, and trial expiry works as before: an
expired trial keeps its plan until ``check_trial_expiry`` moves it to Free.
Seat limits are out of scope and keep reading the subscribed plan.
"""
from __future__ import annotations

import datetime
from datetime import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.feature_catalog import ALL_FEATURE_KEYS, DEFAULT_TURN_INPUT_TOKENS
from app.config import settings
from app.models import Base
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.user import Organization, Role, User
from app.services import feature_service, subscription_service
from app.services.exceptions import ValidationError
from app.services.feature_service import UsageLimit, get_entitlements

NOW = dt(2026, 9, 30, 12, 0, 0)
TODAY = NOW.date()
DAY = datetime.timedelta(days=1)

PRO_FEATURES = {k: True for k in ALL_FEATURE_KEYS}
PRO_LIMITS = {
    "assistant.turns": {"period": "month", "limit": 500},
    "platform_ai.micros": {"period": "month", "limit": 9_000},
}
FREE_FEATURES = {"ai.budget": True}
FREE_LIMITS = {
    "assistant.turns": {"period": "month", "limit": 5},
    "mcp.calls": {"period": "day", "limit": 0},
}
FREE_MAP = {k: k in FREE_FEATURES for k in ALL_FEATURE_KEYS}


@pytest_asyncio.fixture
async def factory(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    finally:
        await eng.dispose()


async def _plans(f, *, with_free: bool = True) -> None:
    async with f() as db:
        db.add(Plan(slug="pro", name="Pro", features=PRO_FEATURES, usage_limits=PRO_LIMITS,
                    max_users=5))
        if with_free:
            db.add(Plan(slug="free", name="Free", features=FREE_FEATURES,
                        usage_limits=FREE_LIMITS, max_users=1))
        await db.commit()


async def _org_on_pro(f, status: SubscriptionStatus, *, trial_end=None, period_end=None) -> int:
    async with f() as db:
        org = Organization(name=f"Org {status.value}", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        pro_id = await db.scalar(select(Plan.id).where(Plan.slug == "pro"))
        db.add(Subscription(
            org_id=org.id, plan_id=pro_id, status=status, trial_start=None, trial_end=trial_end,
            current_period_start=(period_end - 30 * DAY) if period_end else None,
            current_period_end=period_end,
        ))
        await db.commit()
        return org.id


async def _ent(f, org_id, now=NOW):
    async with f() as db:
        return await get_entitlements(db, org_id, now=now)


def _assert_pro(ent) -> None:
    assert ent.features == PRO_FEATURES
    assert ent.plan_features == PRO_FEATURES
    assert ent.limits["assistant.turns"] == UsageLimit("month", 500)
    assert ent.limits["platform_ai.micros"] == UsageLimit("month", 9_000)
    assert ent.frontier_models is True
    assert ent.turn_input_tokens == 60_000
    assert ent.has_plan is True


def _assert_free(ent) -> None:
    assert ent.features == FREE_MAP
    assert ent.plan_features == FREE_MAP
    assert ent.limits["assistant.turns"] == UsageLimit("month", 5)
    assert ent.limits["mcp.calls"] == UsageLimit("day", 0)
    # Free sets no platform meter: the catalog default (none at all) applies.
    assert ent.limits["platform_ai.micros"] == UsageLimit("month", 0)
    assert ent.plan_limits == ent.limits
    # Plan-slug constants follow the effective plan too.
    assert ent.frontier_models is False
    assert ent.turn_input_tokens == DEFAULT_TURN_INPUT_TOKENS
    assert ent.has_plan is True


@pytest.mark.parametrize("status", [SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE])
async def test_trialing_and_active_follow_their_plan(factory, status):
    await _plans(factory)
    _assert_pro(await _ent(factory, await _org_on_pro(factory, status)))


@pytest.mark.parametrize("period_end", [TODAY + 10 * DAY, TODAY])
async def test_canceled_keeps_its_plan_until_the_period_ends(factory, period_end):
    """The cancel promise: access continues through ``current_period_end``.
    Wrong implementation: Free at the moment of cancel."""
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.CANCELED, period_end=period_end)
    _assert_pro(await _ent(factory, org))


async def test_canceled_past_its_period_end_gets_the_free_plan(factory):
    """Wrong implementation: entitlements read off the subscribed plan row
    regardless of status, so a canceled Pro org keeps Pro for good."""
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.CANCELED, period_end=TODAY - DAY)
    _assert_free(await _ent(factory, org))


async def test_canceled_with_no_period_end_gets_the_free_plan_at_once(factory):
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.CANCELED, period_end=None)
    _assert_free(await _ent(factory, org))


@pytest.mark.parametrize("period_end", [None, TODAY - DAY, TODAY + 10 * DAY])
async def test_past_due_gets_the_free_plan_at_once(factory, period_end):
    """Wrong implementation: PAST_DUE given the CANCELED grace period."""
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.PAST_DUE, period_end=period_end)
    _assert_free(await _ent(factory, org))


async def test_the_period_end_is_read_on_the_resolvers_clock(factory):
    """One canceled org, two clocks: before and after its last day."""
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.CANCELED, period_end=TODAY)
    _assert_pro(await _ent(factory, org, now=dt(2026, 9, 30, 23, 59, 59)))
    _assert_free(await _ent(factory, org, now=dt(2026, 10, 1, 0, 0, 0)))


@pytest.mark.parametrize(
    ("status", "period_end"),
    [(SubscriptionStatus.CANCELED, None), (SubscriptionStatus.PAST_DUE, None)],
)
async def test_overrides_still_apply_on_top_of_free(factory, status, period_end):
    await _plans(factory)
    org = await _org_on_pro(factory, status, period_end=period_end)
    async with factory() as db:
        db.add(OrgFeatureOverride(org_id=org, feature_key="ai.agent", value=True))
        await db.commit()
    ent = await _ent(factory, org)
    assert ent.features["ai.agent"] is True
    assert ent.plan_features["ai.agent"] is False
    assert ent.features["ai.forecast"] is False
    assert ent.overridden == {"ai.agent"}


@pytest.mark.parametrize("status", [SubscriptionStatus.CANCELED, SubscriptionStatus.PAST_DUE])
async def test_missing_free_plan_falls_back_to_catalog_defaults(factory, status):
    """Wrong implementation: no Free row read as "keep the subscribed plan"."""
    await _plans(factory, with_free=False)
    org = await _org_on_pro(factory, status)
    ent = await _ent(factory, org)
    assert ent.features == {k: False for k in ALL_FEATURE_KEYS}
    assert ent.has_plan is False
    assert ent.frontier_models is False


async def test_has_feature_is_closed_for_a_lapsed_org(factory):
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.PAST_DUE)
    async with factory() as db:
        assert await feature_service.has_feature(db, org, "ai.agent") is False
        assert await feature_service.has_feature(db, org, "ai.budget") is True


async def test_expired_trial_keeps_its_plan_until_the_expiry_sweep(factory):
    """Trials work as before: TRIALING past ``trial_end`` keeps its plan until
    ``check_trial_expiry`` moves it to Free (ACTIVE), which then resolves as
    Free through the plan row, not through the status."""
    await _plans(factory)
    yesterday = datetime.date.today() - DAY
    org = await _org_on_pro(factory, SubscriptionStatus.TRIALING, trial_end=yesterday)
    assert (await _ent(factory, org)).features == PRO_FEATURES

    async with factory() as db:
        sub = await subscription_service.check_trial_expiry(db, org)
        assert sub.status == SubscriptionStatus.ACTIVE
    ent = await _ent(factory, org)
    assert ent.features == FREE_MAP
    assert ent.limits["assistant.turns"] == UsageLimit("month", 5)


async def test_cancel_keeps_access_until_the_period_ends(factory):
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.ACTIVE, period_end=TODAY + 5 * DAY)
    async with factory() as db:
        await subscription_service.cancel_subscription(db, org)
    _assert_pro(await _ent(factory, org))
    _assert_free(await _ent(factory, org, now=NOW + 6 * DAY))


async def test_seat_limit_still_reads_the_subscribed_plan(factory, monkeypatch):
    """Seat limits wait for billing (owner decision): a lapsed Pro org is
    still limited by Pro's ``max_users``, not Free's, once billing is live."""
    monkeypatch.setattr(settings, "billing_ui_enabled", True)
    await _plans(factory)
    org = await _org_on_pro(factory, SubscriptionStatus.CANCELED)
    async with factory() as db:
        for i in range(2):
            db.add(User(org_id=org, username=f"u{i}", email=f"u{i}@example.com",
                        password_hash="x", role=Role.MEMBER, is_active=True))
        await db.commit()
        # Two active users: over Free's 1, under Pro's 5.
        await subscription_service.enforce_user_limit(db, org)
    async with factory() as db:
        for i in range(2, 5):
            db.add(User(org_id=org, username=f"u{i}", email=f"u{i}@example.com",
                        password_hash="x", role=Role.MEMBER, is_active=True))
        await db.commit()
        with pytest.raises(ValidationError):
            await subscription_service.enforce_user_limit(db, org)
