"""Tests for the ApiToken model (superadmin PAT feature, spec 2026-07-2x).

Uses an in-memory aiosqlite engine (project convention, same pattern as
test_email_broadcast_model.py) so no running MySQL / docker-compose stack is
required. The real MySQL DDL check (unique index + FK-cover) is a separate
manual merge gate, not this unit test.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import ApiToken, Base
from app.models.user import Organization, Role, User


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def seed_superadmin(session_factory):
    async with session_factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            username="admin1",
            email="admin@x.io",
            password_hash="hashed",
            org_id=org.id,
            role=Role.OWNER,
            is_superadmin=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_create_api_token(session_factory, seed_superadmin):
    async with session_factory() as db:
        tok = ApiToken(
            token_hash="a" * 64,
            token_prefix="pat_abcdefghij",
            name="cron",
            scope="write",
            created_by_user_id=seed_superadmin.id,
            created_by_email=seed_superadmin.email,
            expires_at=dt.datetime.utcnow() + dt.timedelta(days=30),
        )
        db.add(tok)
        await db.commit()
        await db.refresh(tok)

        assert tok.id is not None
        assert tok.revoked_at is None
        assert tok.reminder_stage == 0
        assert tok.last_used_at is None
        assert tok.last_used_ip is None
        assert tok.created_at is not None


@pytest.mark.asyncio
async def test_token_hash_unique(session_factory, seed_superadmin):
    async with session_factory() as db:
        common = dict(
            token_hash="b" * 64,
            token_prefix="pat_bbbbbbbbbb",
            name="dup",
            scope="read",
            created_by_user_id=seed_superadmin.id,
            created_by_email=seed_superadmin.email,
            expires_at=dt.datetime.utcnow() + dt.timedelta(days=30),
        )
        db.add(ApiToken(**common))
        await db.commit()

        db.add(ApiToken(**{**common, "token_prefix": "pat_cccccccccc"}))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_created_by_user_set_null_on_user_delete(session_factory, seed_superadmin):
    async with session_factory() as db:
        tok = ApiToken(
            token_hash="c" * 64,
            token_prefix="pat_dddddddddd",
            name="survives-user-delete",
            scope="read",
            created_by_user_id=seed_superadmin.id,
            created_by_email=seed_superadmin.email,
            expires_at=dt.datetime.utcnow() + dt.timedelta(days=30),
        )
        db.add(tok)
        await db.commit()
        await db.refresh(tok)
        tok_id = tok.id

        user = await db.get(User, seed_superadmin.id)
        await db.delete(user)
        await db.commit()

        # The DB-level ON DELETE SET NULL fired in the database, not via
        # ORM cascade — the identity map still holds the stale pre-delete
        # `tok` object, so force a fresh read.
        db.expire_all()
        refreshed = await db.get(ApiToken, tok_id)
        assert refreshed.created_by_user_id is None
        # Email snapshot survives the FK going NULL.
        assert refreshed.created_by_email == "admin@x.io"
