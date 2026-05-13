"""Router tests for L4.5 admin subscriptions endpoints.

Pins:

- Auth gate (``subscriptions.view`` → superadmin short-circuits;
  non-superadmin → 403).
- Envelope shapes for list / detail / kpis.
- ``mock_revenue`` flag is True (until L2 wires real billing).
- Audit row is written via ``audit_service.record_audit_event``
  on a list / detail hit (throttle fails open in tests).
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.routers.admin_subscriptions import router as admin_subscriptions_router
from app.security import hash_password


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


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_subscriptions_router)
    return app


async def _seed(factory) -> dict:
    """One superadmin org + one target org, both with subscriptions."""
    async with factory() as db:
        free = Plan(
            slug="free", name="Free", description="",
            price_monthly=Decimal("0.00"), price_yearly=Decimal("0.00"),
        )
        pro = Plan(
            slug="pro", name="Pro", description="",
            price_monthly=Decimal("9.99"), price_yearly=Decimal("99.00"),
        )
        db.add_all([free, pro])
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(
            org_id=admin_org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=target.id, username="member",
            email="m@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        admin_sub = Subscription(
            org_id=admin_org.id, plan_id=free.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        target_sub = Subscription(
            org_id=target.id, plan_id=pro.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_end=datetime.date.today() + datetime.timedelta(days=10),
        )
        db.add_all([admin_sub, target_sub])
        await db.commit()
        return {
            "target_sub_id": target_sub.id,
            "admin_sub_id": admin_sub.id,
            "target_org_id": target.id,
        }


def _superadmin_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(False)))
            ).scalar_one()
    return resolve


# ── auth gates ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_403_for_non_superadmin(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_detail_403_for_non_superadmin(session_factory):
    seeded = await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/subscriptions/{seeded['target_sub_id']}"
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_kpis_403_for_non_superadmin(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions/kpis")
    assert res.status_code == 403


# ── happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_envelope_for_superadmin(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    org_names = {row["org_name"] for row in body["items"]}
    assert org_names == {"Admin Org", "Target Inc"}


@pytest.mark.asyncio
async def test_list_status_filter_via_query(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions?status=trialing")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "trialing"


@pytest.mark.asyncio
async def test_list_rejects_unknown_status_with_422(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions?status=bogus")
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_detail_returns_full_envelope(session_factory):
    seeded = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/admin/subscriptions/{seeded['target_sub_id']}"
        )
    assert res.status_code == 200
    body = res.json()
    assert body["subscription_id"] == seeded["target_sub_id"]
    assert body["org"]["name"] == "Target Inc"
    assert body["plan"]["slug"] == "pro"
    assert body["mock_revenue"] is True
    assert body["mock_revenue_amount"] == "0.00"


@pytest.mark.asyncio
async def test_detail_404_for_unknown_subscription(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions/99999")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_kpis_returns_counts_and_mock_flag(session_factory):
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/subscriptions/kpis")
    assert res.status_code == 200
    body = res.json()
    assert body["total_subscriptions"] == 2
    assert body["active"] == 1
    assert body["trial"] == 1
    assert body["mock_revenue"] is True
    assert body["mock_mrr"] == "0.00"


# ── audit wiring ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_emits_durable_audit_row_when_unthrottled(session_factory):
    """When the throttle returns True (first hit in the window), the
    list endpoint writes a durable audit row via
    ``audit_service.record_audit_event``."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with patch(
        "app.routers.admin_subscriptions._should_persist_audit",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.routers.admin_subscriptions.audit_service.record_audit_event",
        new=AsyncMock(),
    ) as record:
        with TestClient(app) as client:
            res = client.get("/api/v1/admin/subscriptions")
        assert res.status_code == 200
        assert record.await_count == 1
        kwargs = record.await_args.kwargs
        assert kwargs["event_type"] == "admin.subscriptions.viewed"
        assert kwargs["outcome"] == "success"
        assert kwargs["detail"]["view"] == "list"


@pytest.mark.asyncio
async def test_detail_emits_durable_audit_with_target_org(session_factory):
    seeded = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with patch(
        "app.routers.admin_subscriptions._should_persist_audit",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.routers.admin_subscriptions.audit_service.record_audit_event",
        new=AsyncMock(),
    ) as record:
        with TestClient(app) as client:
            res = client.get(
                f"/api/v1/admin/subscriptions/{seeded['target_sub_id']}"
            )
        assert res.status_code == 200
        assert record.await_count == 1
        kwargs = record.await_args.kwargs
        # Detail uses a distinct event type from list so the throttle
        # doesn't suppress this row when an admin drills in within 60s
        # of viewing the list.
        assert kwargs["event_type"] == "admin.subscriptions.detail.viewed"
        assert kwargs["target_org_id"] == seeded["target_org_id"]
        assert kwargs["target_org_name"] == "Target Inc"
        assert kwargs["detail"]["view"] == "detail"


@pytest.mark.asyncio
async def test_list_audit_detail_does_not_leak_raw_query(session_factory):
    """Privacy pin: ``q`` is NEVER stored in the durable audit detail.
    Only ``query_length`` / ``has_query`` should be present. Mirrors
    the admin_users.py contract."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    raw_q = "secret-search-string"
    with patch(
        "app.routers.admin_subscriptions._should_persist_audit",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.routers.admin_subscriptions.audit_service.record_audit_event",
        new=AsyncMock(),
    ) as record:
        with TestClient(app) as client:
            res = client.get(f"/api/v1/admin/subscriptions?q={raw_q}")
        assert res.status_code == 200
        kwargs = record.await_args.kwargs
        filters = kwargs["detail"]["filters"]
        assert "q" not in filters
        assert filters["query_length"] == len(raw_q)
        assert filters["has_query"] is True


@pytest.mark.asyncio
async def test_detail_audit_not_suppressed_after_recent_list_view(session_factory):
    """The throttle is keyed on (event_type, actor); list and detail
    use different event types, so a recent list view must not suppress
    the detail audit row. We simulate by returning True for both calls
    (the real throttle would, since the keys differ)."""
    seeded = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with patch(
        "app.routers.admin_subscriptions._should_persist_audit",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.routers.admin_subscriptions.audit_service.record_audit_event",
        new=AsyncMock(),
    ) as record:
        with TestClient(app) as client:
            assert client.get("/api/v1/admin/subscriptions").status_code == 200
            assert client.get(
                f"/api/v1/admin/subscriptions/{seeded['target_sub_id']}"
            ).status_code == 200
        assert record.await_count == 2
        event_types = [c.kwargs["event_type"] for c in record.await_args_list]
        assert event_types == [
            "admin.subscriptions.viewed",
            "admin.subscriptions.detail.viewed",
        ]


@pytest.mark.asyncio
async def test_audit_throttle_skips_durable_row_when_redis_says_no(session_factory):
    """When the throttle helper returns False (Redis SET NX failed
    because the key was already set inside the 60s window), the
    durable audit row is skipped — structlog event still fires
    elsewhere as the fallback channel."""
    await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with patch(
        "app.routers.admin_subscriptions._should_persist_audit",
        new=AsyncMock(return_value=False),
    ), patch(
        "app.routers.admin_subscriptions.audit_service.record_audit_event",
        new=AsyncMock(),
    ) as record:
        with TestClient(app) as client:
            res = client.get("/api/v1/admin/subscriptions")
            assert res.status_code == 200
            res2 = client.get("/api/v1/admin/subscriptions")
            assert res2.status_code == 200
        assert record.await_count == 0
