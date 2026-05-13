"""Service-layer tests for L4.5 admin_subscription_service.

Pins:

- ``list_subscriptions`` paginates, filters by status / plan / query
  and joins org + plan names into the row shape.
- ``get_subscription_detail`` returns the full envelope including
  read-only feature-override snapshots from L4.11.
- ``aggregate_revenue_kpis`` emits the seven counts and the plan
  distribution; ``mock_revenue`` flag is always True and dollar
  figures are mock zeros until L2.2 wires real billing.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app._time import utcnow_naive
from app.models import Base
from app.models.feature_override import OrgFeatureOverride
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services import admin_subscription_service
from app.services.exceptions import NotFoundError


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_plans(db: AsyncSession) -> dict[str, Plan]:
    free = Plan(
        slug="free", name="Free", description="",
        price_monthly=Decimal("0.00"), price_yearly=Decimal("0.00"),
    )
    pro = Plan(
        slug="pro", name="Pro", description="",
        price_monthly=Decimal("9.99"), price_yearly=Decimal("99.00"),
    )
    db.add_all([free, pro])
    await db.commit()
    await db.refresh(free)
    await db.refresh(pro)
    return {"free": free, "pro": pro}


async def _seed_org(db: AsyncSession, name: str) -> Organization:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _seed_sub(
    db: AsyncSession,
    *,
    org: Organization,
    plan: Plan,
    status: SubscriptionStatus,
    trial_end: datetime.date | None = None,
) -> Subscription:
    sub = Subscription(
        org_id=org.id,
        plan_id=plan.id,
        status=status,
        billing_interval=BillingInterval.MONTHLY,
        trial_end=trial_end,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


# ── list_subscriptions ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_joined_org_and_plan(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    acme = await _seed_org(db_session, "Acme")
    await _seed_sub(
        db_session, org=acme, plan=plans["pro"],
        status=SubscriptionStatus.ACTIVE,
    )

    out = await admin_subscription_service.list_subscriptions(db_session)
    assert out["total"] == 1
    item = out["items"][0]
    assert item["org_name"] == "Acme"
    assert item["plan_slug"] == "pro"
    assert item["plan_name"] == "Pro"
    assert item["status"] == "active"
    assert item["billing_interval"] == "monthly"


@pytest.mark.asyncio
async def test_list_filters_by_status(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    a = await _seed_org(db_session, "A")
    b = await _seed_org(db_session, "B")
    await _seed_sub(db_session, org=a, plan=plans["pro"], status=SubscriptionStatus.ACTIVE)
    await _seed_sub(db_session, org=b, plan=plans["pro"], status=SubscriptionStatus.TRIALING)

    only_trial = await admin_subscription_service.list_subscriptions(
        db_session, status_filter="trialing"
    )
    assert only_trial["total"] == 1
    assert only_trial["items"][0]["org_name"] == "B"


@pytest.mark.asyncio
async def test_list_filters_by_plan(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    a = await _seed_org(db_session, "A")
    b = await _seed_org(db_session, "B")
    await _seed_sub(db_session, org=a, plan=plans["free"], status=SubscriptionStatus.ACTIVE)
    await _seed_sub(db_session, org=b, plan=plans["pro"], status=SubscriptionStatus.ACTIVE)

    only_pro = await admin_subscription_service.list_subscriptions(
        db_session, plan_filter="pro"
    )
    assert only_pro["total"] == 1
    assert only_pro["items"][0]["plan_slug"] == "pro"


@pytest.mark.asyncio
async def test_list_search_matches_org_name(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    a = await _seed_org(db_session, "Acme Co")
    b = await _seed_org(db_session, "Globex Corp")
    await _seed_sub(db_session, org=a, plan=plans["free"], status=SubscriptionStatus.ACTIVE)
    await _seed_sub(db_session, org=b, plan=plans["free"], status=SubscriptionStatus.ACTIVE)

    hits = await admin_subscription_service.list_subscriptions(db_session, q="globex")
    assert hits["total"] == 1
    assert hits["items"][0]["org_name"] == "Globex Corp"


@pytest.mark.asyncio
async def test_list_pagination(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    for i in range(5):
        org = await _seed_org(db_session, f"Org{i}")
        await _seed_sub(
            db_session, org=org, plan=plans["free"], status=SubscriptionStatus.ACTIVE
        )

    page1 = await admin_subscription_service.list_subscriptions(
        db_session, limit=2, offset=0
    )
    page2 = await admin_subscription_service.list_subscriptions(
        db_session, limit=2, offset=2
    )
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    # Newest-first ordering: page1 should have higher subscription_ids
    # than page2.
    assert page1["items"][0]["subscription_id"] > page2["items"][0]["subscription_id"]


@pytest.mark.asyncio
async def test_list_invalid_status_raises_for_direct_callers(db_session: AsyncSession):
    """Router-level Literal rejects unknown values with 422; direct
    service callers (and tests) get a ValueError."""
    with pytest.raises(ValueError, match="Unknown subscription status"):
        await admin_subscription_service.list_subscriptions(
            db_session, status_filter="bogus"
        )


# ── get_subscription_detail ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_includes_org_plan_and_member_count(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    org = await _seed_org(db_session, "Acme")
    db_session.add_all([
        User(
            org_id=org.id, username=f"u{i}", email=f"u{i}@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_active=True, email_verified=True,
        )
        for i in range(3)
    ])
    await db_session.commit()
    sub = await _seed_sub(
        db_session, org=org, plan=plans["pro"],
        status=SubscriptionStatus.ACTIVE,
    )

    detail = await admin_subscription_service.get_subscription_detail(
        db_session, subscription_id=sub.id
    )
    assert detail["subscription_id"] == sub.id
    assert detail["org"]["name"] == "Acme"
    assert detail["org"]["member_count"] == 3
    assert detail["plan"]["slug"] == "pro"
    assert detail["mock_revenue"] is True
    assert detail["mock_revenue_amount"] == "0.00"
    assert detail["feature_overrides"] == []


@pytest.mark.asyncio
async def test_detail_includes_feature_overrides(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    org = await _seed_org(db_session, "Acme")
    sub = await _seed_sub(
        db_session, org=org, plan=plans["pro"],
        status=SubscriptionStatus.ACTIVE,
    )
    now = utcnow_naive()
    db_session.add_all([
        OrgFeatureOverride(
            org_id=org.id, feature_key="ai.budget", value=True,
            set_by=None, set_at=now,
            expires_at=None, note="comped",
        ),
        OrgFeatureOverride(
            org_id=org.id, feature_key="ai.forecast", value=True,
            set_by=None, set_at=now,
            expires_at=now - datetime.timedelta(days=1),  # expired
            note="trial expired",
        ),
    ])
    await db_session.commit()

    detail = await admin_subscription_service.get_subscription_detail(
        db_session, subscription_id=sub.id
    )
    overrides = {o["feature_key"]: o for o in detail["feature_overrides"]}
    assert overrides["ai.budget"]["is_expired"] is False
    assert overrides["ai.forecast"]["is_expired"] is True


@pytest.mark.asyncio
async def test_detail_404_when_missing(db_session: AsyncSession):
    with pytest.raises(NotFoundError):
        await admin_subscription_service.get_subscription_detail(
            db_session, subscription_id=999
        )


# ── aggregate_revenue_kpis ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kpis_counts_by_status(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    today = datetime.date.today()

    # 2 active, 1 trialing (expiring in 3 days), 1 past_due, 1 cancelled.
    for status_, trial_end in [
        (SubscriptionStatus.ACTIVE, None),
        (SubscriptionStatus.ACTIVE, None),
        (SubscriptionStatus.TRIALING, today + datetime.timedelta(days=3)),
        (SubscriptionStatus.PAST_DUE, None),
        (SubscriptionStatus.CANCELED, None),
    ]:
        org = await _seed_org(db_session, f"O-{status_.value}-{trial_end}")
        await _seed_sub(
            db_session, org=org, plan=plans["pro"], status=status_, trial_end=trial_end
        )

    kpis = await admin_subscription_service.aggregate_revenue_kpis(db_session)
    assert kpis["total_subscriptions"] == 5
    assert kpis["active"] == 2
    assert kpis["trial"] == 1
    assert kpis["past_due"] == 1
    assert kpis["cancelled"] == 1
    assert kpis["trial_expiring_next_7d"] == 1
    assert kpis["mock_revenue"] is True
    assert kpis["mock_mrr"] == "0.00"
    assert kpis["mock_arr"] == "0.00"


@pytest.mark.asyncio
async def test_kpis_plan_distribution_includes_zero_plans(db_session: AsyncSession):
    """A plan with zero subscriptions still appears in the distribution
    so the FE can render empty rows without re-mapping the canonical
    plan list."""
    plans = await _seed_plans(db_session)
    a = await _seed_org(db_session, "A")
    await _seed_sub(
        db_session, org=a, plan=plans["pro"], status=SubscriptionStatus.ACTIVE,
    )

    kpis = await admin_subscription_service.aggregate_revenue_kpis(db_session)
    dist = {d["plan_slug"]: d for d in kpis["plan_distribution"]}
    assert dist["pro"]["subscription_count"] == 1
    assert dist["free"]["subscription_count"] == 0


@pytest.mark.asyncio
async def test_kpis_trial_expiring_excludes_already_expired(db_session: AsyncSession):
    plans = await _seed_plans(db_session)
    today = datetime.date.today()
    a = await _seed_org(db_session, "A")
    b = await _seed_org(db_session, "B")
    # Past trial end: NOT counted.
    await _seed_sub(
        db_session, org=a, plan=plans["pro"],
        status=SubscriptionStatus.TRIALING,
        trial_end=today - datetime.timedelta(days=1),
    )
    # Future-within-window: counted.
    await _seed_sub(
        db_session, org=b, plan=plans["pro"],
        status=SubscriptionStatus.TRIALING,
        trial_end=today + datetime.timedelta(days=5),
    )
    kpis = await admin_subscription_service.aggregate_revenue_kpis(db_session)
    assert kpis["trial_expiring_next_7d"] == 1
