"""TBD-585: plan writes canonicalize usage_limits, are interactive-only and audited."""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.routers.plans import router as plans_router
from app.security import hash_password
from app.services.exceptions import ValidationError
from app.services.plan_service import canonicalize_usage_limits
from tests.factories import make_test_app

DEFAULTS = {
    "assistant.turns": {"period": "month", "limit": None},
    "mcp.calls": {"period": "month", "limit": None},
    "platform_ai.tokens": {"period": "month", "limit": 0},
    "platform_ai.cents": {"period": "month", "limit": 0},
}


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


async def _seed(factory, usage_limits=None) -> int:
    async with factory() as db:
        org = Organization(name="Admin Org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        db.add(User(org_id=org.id, username="root", email="root@platform.io",
                    password_hash=hash_password("pw-1234567"), role=Role.OWNER,
                    is_superadmin=True, is_active=True, email_verified=True))
        plan = Plan(name="Pro", slug="pro", description="", is_custom=False, is_active=True,
                    sort_order=1, price_monthly=Decimal("1"), price_yearly=Decimal("10"),
                    features={"ai.budget": True})
        if usage_limits is not None:
            plan.usage_limits = usage_limits
        db.add(plan)
        await db.commit()
        return plan.id


def _app(factory):
    async def resolve(sf):
        async with sf() as db:
            return (await db.execute(select(User))).scalar_one()
    return make_test_app(factory, routers=plans_router, current_user=resolve,
                         override_session_factory=True)


async def _plan(factory, slug):
    async with factory() as db:
        return (await db.execute(select(Plan).where(Plan.slug == slug))).scalar_one()


async def _audit(factory, event_type):
    async with factory() as db:
        return (await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )).scalars().all()


def _new(slug="new", **extra):
    return {"name": "New", "slug": slug, **extra}


# ── service ────────────────────────────────────────────────────────────────


def test_canonicalize_per_meter_replace_keeps_the_others():
    existing = {**DEFAULTS, "assistant.turns": {"period": "day", "limit": 10}}
    out = canonicalize_usage_limits({"mcp.calls": {"period": "day", "limit": 5}}, existing)
    assert out["assistant.turns"] == {"period": "day", "limit": 10}
    assert out["mcp.calls"] == {"period": "day", "limit": 5}
    assert out["platform_ai.cents"] == {"period": "month", "limit": 0}


@pytest.mark.parametrize("partial", [
    {"nope": {"period": "day", "limit": 1}},
    {"platform_ai.cents": {"period": "month", "limit": None}},
    {"mcp.calls": {"period": "day"}},                      # wholesale replace, not deep merge
    {"mcp.calls": {"period": "week", "limit": 1}},
    {"mcp.calls": {"period": "day", "limit": "5"}},
    {"mcp.calls": {"period": "day", "limit": -1}},
    {"mcp.calls": {"period": "day", "limit": 1, "x": 1}},
])
def test_canonicalize_rejects_with_validation_error(partial):
    with pytest.raises(ValidationError):
        canonicalize_usage_limits(partial, {"mcp.calls": {"period": "month", "limit": 5}})


# ── create ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_canonicalizes_and_audits(session_factory):
    await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.post("/api/v1/plans", json=_new(
            usage_limits={"mcp.calls": {"period": "day", "limit": 5}}))
        assert r.status_code == 201, r.text
        assert r.json()["usage_limits"] == {**DEFAULTS, "mcp.calls": {"period": "day", "limit": 5}}
        r = c.post("/api/v1/plans", json=_new("bare"))
        assert r.json()["usage_limits"] == DEFAULTS
    assert (await _plan(session_factory, "new")).usage_limits == {
        **DEFAULTS, "mcp.calls": {"period": "day", "limit": 5}}
    rows = await _audit(session_factory, "admin.plan.created")
    assert len(rows) == 2
    d = sorted(rows, key=lambda r: r.id)[0].detail
    assert d["slug"] == "new" and d["plan_id"] and d["usage_limits"]["mcp.calls"]["limit"] == 5
    assert d["features"]["ai.budget"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_limits,code", [
    ({"nope": {"period": "day", "limit": 1}}, 400),
    ({"platform_ai.tokens": {"period": "month", "limit": None}}, 400),
    ({"mcp.calls": {"period": "day", "limit": "5"}}, 400),
    (None, 422),
])
async def test_create_rejects_bad_usage_limits(session_factory, usage_limits, code):
    await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.post("/api/v1/plans", json=_new(usage_limits=usage_limits))
    assert r.status_code == code
    assert await _audit(session_factory, "admin.plan.created") == []


# ── update ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_replaces_per_meter_not_whole_dict(session_factory):
    pid = await _seed(session_factory, {**DEFAULTS, "assistant.turns": {"period": "day", "limit": 10}})
    with TestClient(_app(session_factory)) as c:
        r = c.put(f"/api/v1/plans/{pid}",
                  json={"usage_limits": {"mcp.calls": {"period": "day", "limit": 7}}})
    assert r.status_code == 200, r.text
    assert r.json()["usage_limits"]["assistant.turns"] == {"period": "day", "limit": 10}
    assert r.json()["usage_limits"]["mcp.calls"] == {"period": "day", "limit": 7}
    assert (await _plan(session_factory, "pro")).usage_limits["assistant.turns"]["limit"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    ({"usage_limits": None}, 422),
    ({"usage_limits": {"nope": {"period": "day", "limit": 1}}}, 400),
    ({"usage_limits": {"platform_ai.cents": {"period": "month", "limit": None}}}, 400),
    ({"usage_limits": {"mcp.calls": {"period": "day"}}}, 400),
])
async def test_update_rejects_bad_usage_limits(session_factory, body, code):
    pid = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        r = c.put(f"/api/v1/plans/{pid}", json=body)
    assert r.status_code == code
    assert await _audit(session_factory, "admin.plan.updated") == []


@pytest.mark.asyncio
async def test_update_audit_lists_changed_fields_and_old_new(session_factory):
    pid = await _seed(session_factory)
    with TestClient(_app(session_factory)) as c:
        c.put(f"/api/v1/plans/{pid}", json={"name": "Pro2"})
        c.put(f"/api/v1/plans/{pid}", json={
            "features": {"ai.budget": False},
            "usage_limits": {"mcp.calls": {"period": "day", "limit": 7}}})
    rows = sorted(await _audit(session_factory, "admin.plan.updated"), key=lambda r: r.id)
    assert len(rows) == 2
    name_only, both = rows[0].detail, rows[1].detail
    assert name_only["changed_fields"] == ["name"] and "old_features" not in name_only
    assert name_only["plan_id"] and name_only["slug"] == "pro"
    assert sorted(both["changed_fields"]) == ["features", "usage_limits"]
    assert both["old_features"]["ai.budget"] is True and both["new_features"]["ai.budget"] is False
    assert both["old_usage_limits"]["mcp.calls"]["limit"] is None
    assert both["new_usage_limits"]["mcp.calls"] == {"period": "day", "limit": 7}


# ── duplicate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_carries_canonical_usage_limits_and_audits(session_factory):
    pid = await _seed(session_factory, {"mcp.calls": {"period": "day", "limit": 3}})  # drifted: partial
    with TestClient(_app(session_factory)) as c:
        r = c.post(f"/api/v1/plans/{pid}/duplicate", json={"name": "Copy", "slug": "copy"})
    assert r.status_code == 201, r.text
    assert (await _plan(session_factory, "copy")).usage_limits == {
        **DEFAULTS, "mcp.calls": {"period": "day", "limit": 3}}
    rows = await _audit(session_factory, "admin.plan.duplicated")
    assert len(rows) == 1
    assert rows[0].detail["source_plan_id"] == pid and rows[0].detail["slug"] == "copy"


@pytest.mark.asyncio
async def test_get_plan_canonicalizes_a_legacy_empty_object(session_factory):
    pid = await _seed(session_factory, {})
    with TestClient(_app(session_factory)) as c:
        assert c.get(f"/api/v1/plans/{pid}").json()["usage_limits"] == DEFAULTS
