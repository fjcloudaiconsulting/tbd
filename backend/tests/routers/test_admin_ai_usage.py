"""Admin AI usage debug endpoint tests (PR2 of AI tier train).

Pins:

- Superadmin GET returns aggregated usage for the period.
- Non-superadmin -> 403.
- Cross-org isolation: superadmin can read any org; regular admin cannot.
- Period parse rejects bad shapes (400).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

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
from app.deps import get_current_user
from app.models import Base
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.user import Organization, Role, User
from app.routers.admin_ai_usage import router as admin_router
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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_router)
    return app


async def _seed(session_factory) -> tuple[int, int, int]:
    """Insert two orgs + ledger rows for the first one.

    Returns (target_org_id, superadmin_user_id, regular_admin_user_id).
    """
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        other = Organization(name="Other", billing_cycle_day=1)
        db.add(other)
        await db.commit()

        superadmin = User(
            org_id=org.id,
            username="super",
            email="super@admin.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            is_superadmin=True,
        )
        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@acme.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
            is_superadmin=False,
        )
        db.add_all([superadmin, admin])
        await db.commit()

        # Ledger rows for org in May 2026.
        rows = [
            AIUsageLedger(
                org_id=org.id,
                credential_id=None,
                feature_key="chat",
                model="gpt-4o-mini",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                est_cost_cents=5,
                latency_ms=42,
                success=True,
                error_class=None,
                dispatched_at=datetime(2026, 5, 10, 12, 0, 0),
            ),
            AIUsageLedger(
                org_id=org.id,
                credential_id=None,
                feature_key="chat",
                model="gpt-4o-mini",
                prompt_tokens=200,
                completion_tokens=80,
                total_tokens=280,
                est_cost_cents=10,
                latency_ms=44,
                success=True,
                error_class=None,
                dispatched_at=datetime(2026, 5, 15, 12, 0, 0),
            ),
            AIUsageLedger(
                org_id=org.id,
                credential_id=None,
                feature_key="smart_forecast",
                model="claude-haiku-4-5",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                est_cost_cents=0,
                latency_ms=10,
                success=False,
                error_class="provider_status_500",
                dispatched_at=datetime(2026, 5, 20, 12, 0, 0),
            ),
            # Out-of-period row (April).
            AIUsageLedger(
                org_id=org.id,
                credential_id=None,
                feature_key="chat",
                model="gpt-4o-mini",
                prompt_tokens=999,
                completion_tokens=999,
                total_tokens=1998,
                est_cost_cents=999,
                latency_ms=10,
                success=True,
                error_class=None,
                dispatched_at=datetime(2026, 4, 30, 23, 59, 59),
            ),
        ]
        db.add_all(rows)
        await db.commit()
        return (org.id, superadmin.id, admin.id)


async def _get_user(session_factory, user_id: int) -> User:
    from sqlalchemy import select

    async with session_factory() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


@pytest.mark.asyncio
async def test_superadmin_can_aggregate_usage(session_factory):
    org_id, super_id, _ = await _seed(session_factory)

    async def resolver(factory):
        return await _get_user(factory, super_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get(f"/api/v1/admin/ai/usage?org_id={org_id}&period=2026-05")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == org_id
    assert body["period"] == "2026-05"
    assert body["total_prompt_tokens"] == 300
    assert body["total_completion_tokens"] == 130
    assert body["total_cost_cents"] == 15  # 5 + 10 (+ 0 for failed)
    assert body["total_calls"] == 3
    by_feature = {row["feature_key"]: row for row in body["by_feature"]}
    assert by_feature["chat"]["calls"] == 2
    assert by_feature["chat"]["failed_calls"] == 0
    assert by_feature["smart_forecast"]["calls"] == 1
    assert by_feature["smart_forecast"]["failed_calls"] == 1
    # April row is NOT counted.
    assert "999" not in str(body)


@pytest.mark.asyncio
async def test_regular_admin_gets_403(session_factory):
    org_id, _, admin_id = await _seed(session_factory)

    async def resolver(factory):
        return await _get_user(factory, admin_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get(f"/api/v1/admin/ai/usage?org_id={org_id}&period=2026-05")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_superadmin_can_read_other_orgs(session_factory):
    """Cross-org reads are the whole point — superadmin debug tool."""
    org_id, super_id, _ = await _seed(session_factory)
    # Confirm we can also read org_id+1 (the "Other" org seeded above)
    # and get an empty aggregation back rather than 403.
    async def resolver(factory):
        return await _get_user(factory, super_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get(
        f"/api/v1/admin/ai/usage?org_id={org_id + 1}&period=2026-05"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_calls"] == 0
    assert body["by_feature"] == []


@pytest.mark.asyncio
async def test_bad_period_returns_400(session_factory):
    org_id, super_id, _ = await _seed(session_factory)

    async def resolver(factory):
        return await _get_user(factory, super_id)

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.get(
        f"/api/v1/admin/ai/usage?org_id={org_id}&period=2026/05"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "invalid_period_format"
