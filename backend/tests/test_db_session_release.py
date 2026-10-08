"""INFRA-128: the request's DB session is released before background tasks run.

Since FastAPI 0.118 a request-scoped ``yield`` dependency exits only after
``await response(...)``, and Starlette runs BackgroundTasks inside that call.
Without the fix ``get_db`` kept the session, and the pooled connection of any
open transaction, for as long as a background email send took (up to 20 s).

These run the real ``get_db`` (no dependency override) against a pooled
SQLite engine, so ``pool.checkedout()`` counts what the request still holds.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import object_session
from sqlalchemy.pool import AsyncAdaptedQueuePool

import app.database as database
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_router
from app.security import create_access_token


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/release.db", poolclass=AsyncAdaptedQueuePool
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        database, "async_session",
        async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False),
    )
    limiter.reset()
    yield eng
    limiter.reset()
    await eng.dispose()


async def _seed_user() -> User:
    async with database.async_session() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="alice", email="alice@acme.io",
            password_hash="x", role=Role.OWNER, is_active=True,
        )
        db.add(user)
        await db.commit()
        return user


async def test_forgot_password_releases_connection_before_email_task(engine, monkeypatch):
    # forgot-password only reads, so its autobegun transaction stays open
    # until the session closes: the case that held a connection.
    await _seed_user()

    seen: list[int] = []

    async def fake_send(email, token):
        seen.append(engine.pool.checkedout())

    monkeypatch.setattr(auth_router, "send_password_reset_email", fake_send)
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/forgot-password", json={"email": "alice@acme.io"}
        )

    assert resp.status_code == 200
    assert seen == [0]


async def test_get_current_user_shares_the_route_session(engine):
    # get_current_user and the route both take Depends(get_db): one session, or
    # the route's commit misses what the auth dependency loaded or changed.
    user = await _seed_user()
    app = FastAPI()
    app.dependency_overrides[get_session_factory] = lambda: database.async_session

    @app.get("/same")
    async def same(me: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
        return {"same": object_session(me) is db.sync_session}

    token = create_access_token(user.id, user.org_id, "owner")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/same", headers={"Authorization": f"Bearer {token}"})

    assert resp.json() == {"same": True}
