"""PUT /api/v1/settings validation for forecast_input_granularity.

The forecast build-granularity setting is a closed enum (master|subcategory).
The service layer defends by falling back to master on garbage, but the router
rejects an out-of-enum write so an admin can't silently persist a value that
will be ignored. See spec 2026-06-01-forecast-subcategory-items.md.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.settings import OrgSetting
from app.models.user import Organization, Role, User
from app.routers.settings import router as settings_router
from app.security import hash_password
from app.services.settings_service import FORECAST_INPUT_GRANULARITY_KEY


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


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(settings_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_active=True, email_verified=True,
        )
        db.add(owner)
        await db.commit()
        return {"org": org.id, "owner": owner.id}


async def _get_user(factory, user_id: int) -> User:
    async with factory() as db:
        return (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one()


@pytest.mark.asyncio
async def test_put_granularity_accepts_subcategory(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put(
        "/api/v1/settings",
        json={"key": FORECAST_INPUT_GRANULARITY_KEY, "value": "subcategory"},
    )
    assert resp.status_code == 200
    async with session_factory() as db:
        val = (
            await db.execute(
                select(OrgSetting.value).where(
                    OrgSetting.org_id == ids["org"],
                    OrgSetting.key == FORECAST_INPUT_GRANULARITY_KEY,
                )
            )
        ).scalar_one()
    assert val == "subcategory"


@pytest.mark.asyncio
async def test_put_granularity_rejects_garbage(session_factory):
    ids = await _seed(session_factory)

    async def resolver(_f):
        return await _get_user(session_factory, ids["owner"])

    client = TestClient(_make_app(session_factory, resolver))
    resp = client.put(
        "/api/v1/settings",
        json={"key": FORECAST_INPUT_GRANULARITY_KEY, "value": "monthly"},
    )
    assert resp.status_code == 400
    # Nothing persisted.
    async with session_factory() as db:
        row = (
            await db.execute(
                select(OrgSetting).where(
                    OrgSetting.org_id == ids["org"],
                    OrgSetting.key == FORECAST_INPUT_GRANULARITY_KEY,
                )
            )
        ).scalar_one_or_none()
    assert row is None
