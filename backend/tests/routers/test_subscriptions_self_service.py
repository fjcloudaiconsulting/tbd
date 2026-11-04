"""BE-05: owner self-service plan changes stay inside the public catalogue.

- ``PUT /api/v1/subscriptions/plan`` is refused while ``billing_ui_enabled``
  is off (the only UI caller, ``/settings/billing``, never calls it then).
- With billing on, a custom plan made for another customer answers 404, the
  same as an unknown slug; the org's own custom plan stays selectable.
- Plan changes and cancellations write an audit row.
- ``GET /api/v1/plans`` hides other customers' custom plans from tenants.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan, Subscription, SubscriptionStatus
from app.models.user import Organization, Role, User
from app.routers.plans import router as plans_router
from app.routers.subscriptions import router as subscriptions_router
from app.services import subscription_service
from app.services.exceptions import NotFoundError


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def billing_on(monkeypatch):
    monkeypatch.setattr(
        "app.routers.subscriptions.app_settings.billing_ui_enabled", True
    )


@pytest.fixture
def billing_off(monkeypatch):
    monkeypatch.setattr(
        "app.routers.subscriptions.app_settings.billing_ui_enabled", False
    )


async def _seed(factory, *, own_plan: str = "pro") -> dict:
    async with factory() as db:
        free = Plan(name="Free", slug="free", sort_order=0, max_users=1)
        pro = Plan(name="Pro", slug="pro", sort_order=1, price_monthly=9, max_users=5)
        theirs = Plan(name="Their deal", slug="their-deal", is_custom=True, sort_order=2)
        ours = Plan(name="Our deal", slug="our-deal", is_custom=True, sort_order=3)
        db.add_all([free, pro, theirs, ours])
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        plan_ids = {p.slug: p.id for p in (free, pro, theirs, ours)}
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io",
            password_hash="x", role=Role.OWNER, is_active=True,
        )
        admin = User(
            org_id=org.id, username="opsadmin", email="ops@platform.io",
            password_hash="x", role=Role.MEMBER, is_active=True, is_superadmin=True,
        )
        db.add_all([owner, admin])
        today = date.today()
        db.add(Subscription(
            org_id=org.id, plan_id=plan_ids[own_plan],
            status=SubscriptionStatus.TRIALING,
            trial_start=today, trial_end=today + timedelta(days=14),
        ))
        await db.commit()
        return {"org_id": org.id, "plan_ids": plan_ids}


def _app(factory, username: str) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_user(db: AsyncSession = Depends(get_db)) -> User:
        return (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = lambda: factory

    @app.exception_handler(NotFoundError)
    async def _nf(_req, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    app.include_router(subscriptions_router)
    app.include_router(plans_router)
    return app


async def _plan_slug(factory, org_id: int) -> str:
    async with factory() as db:
        return await db.scalar(
            select(Plan.slug)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.org_id == org_id)
        )


async def _audit(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as db:
        return list((await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )).scalars().all())


# ── PUT /subscriptions/plan ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_plan_refused_while_billing_ui_disabled(session_factory, billing_off):
    seed = await _seed(session_factory, own_plan="free")
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.put("/api/v1/subscriptions/plan", json={"plan_slug": "pro"})
    assert res.status_code == 403, res.text
    assert await _plan_slug(session_factory, seed["org_id"]) == "free"
    assert await _audit(session_factory, "org.subscription.plan.changed") == []


@pytest.mark.asyncio
async def test_change_plan_to_another_customers_custom_plan_is_404(session_factory, billing_on):
    seed = await _seed(session_factory, own_plan="free")
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.put("/api/v1/subscriptions/plan", json={"plan_slug": "their-deal"})
        unknown = client.put("/api/v1/subscriptions/plan", json={"plan_slug": "no-such-plan"})
    assert res.status_code == 404, res.text
    # Indistinguishable from a slug that does not exist.
    assert res.json() == unknown.json()
    assert await _plan_slug(session_factory, seed["org_id"]) == "free"


@pytest.mark.asyncio
async def test_org_on_custom_plan_can_still_change_interval(session_factory, billing_on):
    seed = await _seed(session_factory, own_plan="our-deal")
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.put(
            "/api/v1/subscriptions/plan",
            json={"plan_slug": "our-deal", "billing_interval": "yearly"},
        )
    assert res.status_code == 200, res.text
    assert res.json()["billing_interval"] == "yearly"
    assert await _plan_slug(session_factory, seed["org_id"]) == "our-deal"


@pytest.mark.asyncio
async def test_change_plan_writes_audit_row(session_factory, billing_on):
    seed = await _seed(session_factory, own_plan="free")
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.put("/api/v1/subscriptions/plan", json={"plan_slug": "pro"})
    assert res.status_code == 200, res.text
    rows = await _audit(session_factory, "org.subscription.plan.changed")
    assert len(rows) == 1
    assert rows[0].target_org_id == seed["org_id"]
    assert rows[0].actor_email == "owner@acme.io"
    assert rows[0].detail["old_plan_slug"] == "free"
    assert rows[0].detail["new_plan_slug"] == "pro"
    assert rows[0].detail["old_status"] == "trialing"
    assert rows[0].detail["new_status"] == "active"


@pytest.mark.asyncio
async def test_cancel_writes_audit_row(session_factory, billing_off):
    seed = await _seed(session_factory)
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.post("/api/v1/subscriptions/cancel")
    assert res.status_code == 200, res.text
    rows = await _audit(session_factory, "org.subscription.canceled")
    assert len(rows) == 1
    assert rows[0].target_org_id == seed["org_id"]
    assert rows[0].detail["old_status"] == "trialing"
    assert rows[0].detail["new_status"] == "canceled"


@pytest.mark.asyncio
async def test_service_change_plan_refuses_custom_plan_that_is_not_current(session_factory):
    seed = await _seed(session_factory, own_plan="free")
    async with session_factory() as db:
        with pytest.raises(NotFoundError):
            await subscription_service.change_plan(
                db, seed["org_id"], "their-deal", "monthly"
            )
    assert await _plan_slug(session_factory, seed["org_id"]) == "free"


# ── GET /plans ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plans_list_hides_other_customers_custom_plans(session_factory):
    await _seed(session_factory, own_plan="our-deal")
    with TestClient(_app(session_factory, "owner")) as client:
        res = client.get("/api/v1/plans")
    assert res.status_code == 200, res.text
    assert [p["slug"] for p in res.json()] == ["free", "pro", "our-deal"]


@pytest.mark.asyncio
async def test_plans_list_shows_custom_plans_to_operators(session_factory):
    await _seed(session_factory)
    with TestClient(_app(session_factory, "opsadmin")) as client:
        res = client.get("/api/v1/plans")
    assert res.status_code == 200, res.text
    assert [p["slug"] for p in res.json()] == ["free", "pro", "their-deal", "our-deal"]
