"""Tests for the ``rate_limit_overrides_service`` CRUD (L4.10).

The runtime resolution (user > org, expiry, newest wins) is fenced in
``tests/test_rate_limit_overrides_wiring.py``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
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

from app.models import Base
from app.models.user import Organization, User
from app.services import rate_limit_overrides_service as svc


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_org_user(factory) -> tuple[int, int]:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        await db.refresh(org)
        u = User(
            org_id=org.id,
            username="u",
            email="u@example.com",
            password_hash="x",
            role="owner",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return org.id, u.id


@pytest.mark.asyncio
async def test_create_requires_exactly_one_scope(session_factory):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        with pytest.raises(ValueError):
            await svc.create_override(
                db,
                org_id=org_id,
                user_id=user_id,
                endpoint_pattern="auth.login",
                max_requests=10,
                period_seconds=60,
                expires_at=None,
                created_by_user_id=None,
                note=None,
            )
        with pytest.raises(ValueError):
            await svc.create_override(
                db,
                org_id=None,
                user_id=None,
                endpoint_pattern="auth.login",
                max_requests=10,
                period_seconds=60,
                expires_at=None,
                created_by_user_id=None,
                note=None,
            )


@pytest.mark.asyncio
async def test_update_and_delete_persist(session_factory):
    org_id, _ = await _seed_org_user(session_factory)
    async with session_factory() as db:
        row = await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=10,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        await svc.update_override(
            db, row=row, patch={"endpoint_pattern": "auth.register"}
        )
        assert (await svc.get_by_id(db, row.id)).endpoint_pattern == "auth.register"
        await svc.delete_override(db, row=row)
        assert await svc.get_by_id(db, row.id) is None


@pytest.mark.asyncio
async def test_list_filters_by_scope(session_factory):
    org_id, user_id = await _seed_org_user(session_factory)
    async with session_factory() as db:
        await svc.create_override(
            db,
            org_id=org_id,
            user_id=None,
            endpoint_pattern="auth.login",
            max_requests=10,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
        await svc.create_override(
            db,
            org_id=None,
            user_id=user_id,
            endpoint_pattern="auth.login",
            max_requests=5,
            period_seconds=60,
            expires_at=None,
            created_by_user_id=None,
            note=None,
        )
    async with session_factory() as db:
        org_rows, org_total = await svc.list_overrides(db, org_id=org_id)
        user_rows, user_total = await svc.list_overrides(db, user_id=user_id)
        all_rows, all_total = await svc.list_overrides(db)
    assert org_total == 1
    assert user_total == 1
    assert all_total == 2
    assert org_rows[0].org_id == org_id
    assert user_rows[0].user_id == user_id
