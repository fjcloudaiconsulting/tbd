"""Service-layer tests for L4.11 — feature entitlement resolver.

Resolver semantics (D5): defaults → plan.features → active override.
Override row PRESENCE wins, not row.value truthiness — a row with
value=False correctly denies an otherwise plan-granted feature.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization
from app.services.exceptions import ValidationError
from app.services.feature_service import (
    UnknownFeatureKey,
    get_features,
    has_feature,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers — small inline seed builders. Each test calls these as needed.
# ---------------------------------------------------------------------------

async def _make_org(db: AsyncSession, name: str = "Acme") -> Organization:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


async def _make_plan(
    db: AsyncSession, *, slug: str, name: str, features: dict
) -> Plan:
    plan = Plan(slug=slug, name=name, features=features)
    db.add(plan)
    await db.commit()
    return plan


async def _make_subscription(
    db: AsyncSession, *, org_id: int, plan_id: int
) -> Subscription:
    sub = Subscription(
        org_id=org_id,
        plan_id=plan_id,
        status=SubscriptionStatus.ACTIVE,
        billing_interval=BillingInterval.MONTHLY,
    )
    db.add(sub)
    await db.commit()
    return sub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_false_when_no_subscription(session_factory):
    """Org with no subscription: resolver fails closed (all-False)."""
    async with session_factory() as db:
        org = await _make_org(db, name="NoSub")

        features = await get_features(db, org.id)

        assert features == {
            "ai.budget": False,
            "ai.forecast": False,
            "ai.smart_plan": False,
            "ai.autocategorize": False,
        }


@pytest.mark.asyncio
async def test_plan_grant_resolves_to_true(session_factory):
    """Pro plan with ai.budget=True, ai.forecast=False resolves correctly."""
    async with session_factory() as db:
        org = await _make_org(db, name="ProOrg")
        plan = await _make_plan(
            db,
            slug="pro",
            name="Pro",
            features={
                "ai.budget": True,
                "ai.forecast": False,
                "ai.smart_plan": False,
                "ai.autocategorize": False,
            },
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        assert await has_feature(db, org.id, "ai.budget") is True
        assert await has_feature(db, org.id, "ai.forecast") is False


@pytest.mark.asyncio
async def test_override_grants_above_plan_default(session_factory):
    """Free plan (all-False) + override ai.budget=True → resolver True."""
    async with session_factory() as db:
        org = await _make_org(db, name="FreeOrg")
        plan = await _make_plan(
            db,
            slug="free",
            name="Free",
            features={
                "ai.budget": False,
                "ai.forecast": False,
                "ai.smart_plan": False,
                "ai.autocategorize": False,
            },
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        db.add(OrgFeatureOverride(
            org_id=org.id, feature_key="ai.budget", value=True,
        ))
        await db.commit()

        assert await has_feature(db, org.id, "ai.budget") is True


@pytest.mark.asyncio
async def test_override_deny_beats_plan_grant(session_factory):
    """Pro plan (all-True) + override ai.budget=False → resolver False.

    This locks in the row-presence semantic from D5: an override row
    with value=False correctly denies an otherwise plan-granted feature.
    """
    async with session_factory() as db:
        org = await _make_org(db, name="DenyOrg")
        plan = await _make_plan(
            db,
            slug="pro",
            name="Pro",
            features={
                "ai.budget": True,
                "ai.forecast": True,
                "ai.smart_plan": True,
                "ai.autocategorize": True,
            },
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        db.add(OrgFeatureOverride(
            org_id=org.id, feature_key="ai.budget", value=False,
        ))
        await db.commit()

        assert await has_feature(db, org.id, "ai.budget") is False
        # Sanity: the other plan grants are unaffected.
        assert await has_feature(db, org.id, "ai.forecast") is True


@pytest.mark.asyncio
async def test_expired_override_ignored(session_factory):
    """Expired override is ignored — plan grant wins."""
    async with session_factory() as db:
        org = await _make_org(db, name="ExpiredOrg")
        plan = await _make_plan(
            db,
            slug="pro",
            name="Pro",
            features={
                "ai.budget": True,
                "ai.forecast": False,
                "ai.smart_plan": False,
                "ai.autocategorize": False,
            },
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        db.add(OrgFeatureOverride(
            org_id=org.id,
            feature_key="ai.budget",
            value=False,
            expires_at=datetime.utcnow() - timedelta(days=1),
        ))
        await db.commit()

        assert await has_feature(db, org.id, "ai.budget") is True


@pytest.mark.asyncio
async def test_unknown_key_raises_unknown_feature_key(session_factory):
    """has_feature with an unknown key raises UnknownFeatureKey."""
    async with session_factory() as db:
        org = await _make_org(db, name="UnknownKeyOrg")

        with pytest.raises(UnknownFeatureKey):
            await has_feature(db, org.id, "ai.totally_made_up")


@pytest.mark.asyncio
async def test_unknown_feature_key_not_a_validation_error(session_factory):
    """UnknownFeatureKey must NOT subclass ValidationError.

    It's a programmer error (HTTP 500), not user input (HTTP 400).
    """
    async with session_factory() as db:
        org = await _make_org(db, name="NotValidationErrOrg")

        try:
            await has_feature(db, org.id, "ai.totally_made_up")
        except UnknownFeatureKey as e:
            assert not isinstance(e, ValidationError)
        else:
            pytest.fail("UnknownFeatureKey was not raised")


@pytest.mark.asyncio
async def test_malformed_plan_features_fails_loudly(session_factory):
    """Plan with an unknown features key must fail read-side validation.

    We bypass canonicalization by writing the raw dict directly to the
    Plan row. The resolver's PlanFeatures.model_validate must reject
    `extra="forbid"` keys with a Pydantic ValidationError.
    """
    async with session_factory() as db:
        org = await _make_org(db, name="MalformedOrg")
        plan = await _make_plan(
            db,
            slug="malformed",
            name="Malformed",
            features={"unknown.bogus": True},
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        with pytest.raises(Exception):
            await get_features(db, org.id)


@pytest.mark.asyncio
async def test_defensive_filter_on_override_rows(session_factory):
    """Stale override row with a key no longer in the catalog must not leak.

    Simulates a row predating a catalog key removal — it bypasses
    write-time validation but the resolver's defensive filter drops it.
    """
    async with session_factory() as db:
        org = await _make_org(db, name="StaleOverrideOrg")
        plan = await _make_plan(
            db,
            slug="pro",
            name="Pro",
            features={
                "ai.budget": True,
                "ai.forecast": False,
                "ai.smart_plan": False,
                "ai.autocategorize": False,
            },
        )
        await _make_subscription(db, org_id=org.id, plan_id=plan.id)

        db.add(OrgFeatureOverride(
            org_id=org.id, feature_key="ancient.key", value=True,
        ))
        await db.commit()

        features = await get_features(db, org.id)

        assert "ancient.key" not in features
        assert features["ai.budget"] is True
