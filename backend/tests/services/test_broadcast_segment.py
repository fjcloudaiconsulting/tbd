"""Tests for the ``active_verified`` segment resolution (Task 2, spec
``2026-07-18-admin-email-broadcast-design.md``).

Covers:
- ``count_segment("active_verified")`` counts only active + verified users.
- ``count_segment`` rejects any other segment value with ``ValueError``
  (Ruling 10 — v1 accepts exactly one segment).
- ``iter_segment_users`` returns ``(user_id, email, first_name)`` tuples
  for the same audience, in the same shape materialization will consume.

Uses an in-memory aiosqlite engine (same fixture pattern as
``tests/models/test_email_broadcast_model.py``) so no running MySQL /
docker-compose stack is required.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.user import Organization, Role, User
from app.security import hash_password
from app.services.broadcast_service import count_segment, iter_segment_users


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
async def db_with_users(session_factory):
    """Seed an Org + 4 Users: 2 active+verified, 1 inactive+verified,
    1 active+unverified. Only the first two belong in ``active_verified``.
    """
    async with session_factory() as db:
        org = Organization(name="TestOrg", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        db.add_all(
            [
                User(
                    username="alice",
                    email="alice@x.io",
                    first_name="Alice",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.OWNER,
                    is_active=True,
                    email_verified=True,
                ),
                User(
                    username="bob",
                    email="bob@x.io",
                    first_name="Bob",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.MEMBER,
                    is_active=True,
                    email_verified=True,
                ),
                User(
                    username="carol",
                    email="carol@x.io",
                    first_name="Carol",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.MEMBER,
                    is_active=False,
                    email_verified=True,
                ),
                User(
                    username="dave",
                    email="dave@x.io",
                    first_name="Dave",
                    password_hash=hash_password("pw-1234567"),
                    org_id=org.id,
                    role=Role.MEMBER,
                    is_active=True,
                    email_verified=False,
                ),
            ]
        )
        await db.commit()
        yield db


@pytest.mark.asyncio
async def test_count_segment_active_verified_only(db_with_users):
    assert await count_segment(db_with_users, "active_verified") == 2


@pytest.mark.asyncio
async def test_count_segment_rejects_unknown(db_with_users):
    with pytest.raises(ValueError):
        await count_segment(db_with_users, "everyone")


@pytest.mark.asyncio
async def test_iter_segment_users_returns_active_verified_tuples(db_with_users):
    rows = await iter_segment_users(db_with_users, "active_verified")
    assert {(email, first_name) for _uid, email, first_name in rows} == {
        ("alice@x.io", "Alice"),
        ("bob@x.io", "Bob"),
    }
    assert all(isinstance(uid, int) for uid, _email, _first_name in rows)


@pytest.mark.asyncio
async def test_iter_segment_users_rejects_unknown(db_with_users):
    with pytest.raises(ValueError):
        await iter_segment_users(db_with_users, "everyone")
