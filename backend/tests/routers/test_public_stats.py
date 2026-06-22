"""Route-level coverage for the public founder-count endpoint.

Pins: public (no auth), excludes the configured non-real usernames and
inactive users, and never 500s (Redis absent in tests → direct count).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.public_stats import router as public_stats_router
from app.security import hash_password
from tests.factories import make_test_app


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed(factory) -> None:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        db.add_all(
            [
                # Two real, active founders → counted.
                User(org_id=org.id, username="alice", email="a@acme.io",
                     password_hash=hash_password("pw"), role=Role.OWNER,
                     is_active=True, is_founder=True),
                User(org_id=org.id, username="bob", email="b@acme.io",
                     password_hash=hash_password("pw"), role=Role.MEMBER,
                     is_active=True, is_founder=True),
                # Excluded smoke account → NOT counted.
                User(org_id=org.id, username="pfv_smoke_l05", email="s@acme.io",
                     password_hash=hash_password("pw"), role=Role.MEMBER,
                     is_active=True, is_founder=True),
                # Inactive founder → NOT counted.
                User(org_id=org.id, username="ghost", email="g@acme.io",
                     password_hash=hash_password("pw"), role=Role.MEMBER,
                     is_active=False, is_founder=True),
                # Non-founder → NOT counted.
                User(org_id=org.id, username="late", email="l@acme.io",
                     password_hash=hash_password("pw"), role=Role.MEMBER,
                     is_active=True, is_founder=False),
            ]
        )
        await db.commit()


@pytest.mark.asyncio
async def test_founder_count_excludes_smoke_inactive_and_nonfounders(session_factory):
    await _seed(session_factory)
    app = make_test_app(session_factory, routers=public_stats_router)
    with TestClient(app) as client:
        res = client.get("/api/v1/public/founder-count")
    assert res.status_code == 200, res.text
    # alice + bob only (smoke excluded, inactive excluded, non-founder excluded).
    assert res.json() == {"count": 2}


@pytest.mark.asyncio
async def test_founder_count_is_public(session_factory):
    # No Authorization header — endpoint must still answer.
    app = make_test_app(session_factory, routers=public_stats_router)
    with TestClient(app) as client:
        res = client.get("/api/v1/public/founder-count")
    assert res.status_code == 200, res.text
    assert res.json() == {"count": 0}
