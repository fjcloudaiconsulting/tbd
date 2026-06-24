"""Tests for the DashboardLayout model.

Covers:
- Row insertion and retrieval (id, all columns present).
- JSON column round-trip (layout_json + canvas_filters_json).
- schema_version server_default (= 1).
- owner_user_id UNIQUE constraint raises IntegrityError on duplicate.

Uses an in-memory SQLite engine (same pattern as test_deps.py) so no
running MySQL / docker-compose stack is required.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.dashboard import DashboardLayout
from app.models.user import Organization, User, Role


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session():
    """Fresh in-memory SQLite session with all tables created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


async def _make_org_and_user(session: AsyncSession, suffix: str = "") -> tuple[Organization, User]:
    """Insert a minimal org + user; return both (committed)."""
    org = Organization(name=f"TestOrg{suffix}", billing_cycle_day=1)
    session.add(org)
    await session.flush()

    user = User(
        username=f"user{suffix}",
        email=f"user{suffix}@example.com",
        password_hash="hashed",
        org_id=org.id,
        role=Role.OWNER,
    )
    session.add(user)
    await session.flush()
    return org, user


# ── tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_and_retrieve(session: AsyncSession) -> None:
    """A DashboardLayout row can be inserted and read back with all fields."""
    org, user = await _make_org_and_user(session, "a")

    layout_data = {"widgets": [{"id": "net-worth", "x": 0, "y": 0}]}
    filters_data = {"date_range": "last_30_days"}

    row = DashboardLayout(
        owner_user_id=user.id,
        org_id=org.id,
        layout_json=layout_data,
        canvas_filters_json=filters_data,
    )
    session.add(row)
    await session.commit()

    fetched = await session.scalar(
        select(DashboardLayout).where(DashboardLayout.owner_user_id == user.id)
    )
    assert fetched is not None
    assert fetched.id is not None
    assert fetched.owner_user_id == user.id
    assert fetched.org_id == org.id


@pytest.mark.asyncio
async def test_json_round_trip(session: AsyncSession) -> None:
    """layout_json and canvas_filters_json survive a commit/select cycle."""
    org, user = await _make_org_and_user(session, "b")

    layout_data = {
        "widgets": [
            {"id": "spending-pie", "x": 0, "y": 0, "w": 4, "h": 3},
            {"id": "cash-flow", "x": 4, "y": 0, "w": 8, "h": 3},
        ]
    }
    filters_data = {"account_ids": [1, 2, 3], "currency": "EUR"}

    row = DashboardLayout(
        owner_user_id=user.id,
        org_id=org.id,
        layout_json=layout_data,
        canvas_filters_json=filters_data,
    )
    session.add(row)
    await session.commit()
    row_id = row.id  # capture before expiry

    session.expire_all()
    fetched = await session.scalar(
        select(DashboardLayout).where(DashboardLayout.id == row_id)
    )
    assert fetched is not None
    assert fetched.layout_json == layout_data
    assert fetched.canvas_filters_json == filters_data


@pytest.mark.asyncio
async def test_schema_version_defaults_to_one(session: AsyncSession) -> None:
    """schema_version must default to 1 when not supplied."""
    org, user = await _make_org_and_user(session, "c")

    row = DashboardLayout(
        owner_user_id=user.id,
        org_id=org.id,
        layout_json={},
        canvas_filters_json={},
    )
    session.add(row)
    await session.commit()
    row_id = row.id  # capture before expiry

    session.expire_all()
    fetched = await session.scalar(
        select(DashboardLayout).where(DashboardLayout.id == row_id)
    )
    assert fetched is not None
    assert fetched.schema_version == 1


@pytest.mark.asyncio
async def test_owner_user_id_unique_constraint(session: AsyncSession) -> None:
    """Inserting two rows for the same owner_user_id raises IntegrityError."""
    org, user = await _make_org_and_user(session, "d")

    first = DashboardLayout(
        owner_user_id=user.id,
        org_id=org.id,
        layout_json={"widgets": []},
        canvas_filters_json={},
    )
    session.add(first)
    await session.commit()

    duplicate = DashboardLayout(
        owner_user_id=user.id,
        org_id=org.id,
        layout_json={"widgets": [{"id": "other"}]},
        canvas_filters_json={},
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        await session.commit()
