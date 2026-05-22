"""Caps CRUD service tests (PR1).

PR1 only persists caps; the cap-check + ledger writes ride in PR2.
Tests pin the closed feature-name set + idempotent set semantics + the
soft<=hard validation in the Pydantic schema.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
from app.models.user import Organization
from app.schemas.org_ai_caps import CapsWrite
from app.services import ai_caps_service
from app.services.ai_routing_service import UnknownFeatureName


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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def org(db: AsyncSession) -> Organization:
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    return org


_ACTOR = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid",
    "ip_address": "127.0.0.1",
}


def test_caps_schema_rejects_hard_below_soft():
    with pytest.raises(ValidationError):
        CapsWrite(soft_cap_cents=10000, hard_cap_cents=5000)


def test_caps_schema_allows_both_null():
    # Vestigial empty row is allowed — call site reads as no cap.
    CapsWrite()


async def test_set_default_caps_inserts_then_updates(db, session_factory, org):
    await ai_caps_service.set_default_caps(
        db,
        org_id=org.id,
        soft_cap_cents=5000,
        hard_cap_cents=10000,
        period="monthly",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await db.execute(
        select(OrgAIDefaultCaps).where(OrgAIDefaultCaps.org_id == org.id)
    )
    rows = list(res.scalars().all())
    assert len(rows) == 1
    assert rows[0].soft_cap_cents == 5000
    assert rows[0].hard_cap_cents == 10000

    # Second set updates in place — PK enforces one row per org.
    await ai_caps_service.set_default_caps(
        db,
        org_id=org.id,
        soft_cap_cents=8000,
        hard_cap_cents=15000,
        period="monthly",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await db.execute(
        select(OrgAIDefaultCaps).where(OrgAIDefaultCaps.org_id == org.id)
    )
    rows = list(res.scalars().all())
    assert len(rows) == 1
    assert rows[0].soft_cap_cents == 8000


async def test_set_feature_caps_rejects_unknown_feature(db, session_factory, org):
    with pytest.raises(UnknownFeatureName):
        await ai_caps_service.set_feature_caps(
            db,
            org_id=org.id,
            feature_key="not_a_real_feature",
            soft_cap_cents=100,
            hard_cap_cents=500,
            period="monthly",
            session_factory=session_factory,
            **_ACTOR,
        )


async def test_feature_caps_pk_uniqueness(db, session_factory, org):
    await ai_caps_service.set_feature_caps(
        db,
        org_id=org.id,
        feature_key="categorize_transactions",
        soft_cap_cents=100,
        hard_cap_cents=500,
        period="monthly",
        session_factory=session_factory,
        **_ACTOR,
    )
    await ai_caps_service.set_feature_caps(
        db,
        org_id=org.id,
        feature_key="categorize_transactions",
        soft_cap_cents=200,
        hard_cap_cents=1000,
        period="monthly",
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await db.execute(
        select(OrgAIFeatureCaps).where(OrgAIFeatureCaps.org_id == org.id)
    )
    rows = list(res.scalars().all())
    assert len(rows) == 1
    assert rows[0].soft_cap_cents == 200


async def test_delete_feature_caps_returns_false_when_missing(
    db, session_factory, org
):
    ok = await ai_caps_service.delete_feature_caps(
        db,
        org_id=org.id,
        feature_key="categorize_transactions",
        session_factory=session_factory,
        **_ACTOR,
    )
    assert ok is False


async def test_delete_feature_caps_removes_row(db, session_factory, org):
    await ai_caps_service.set_feature_caps(
        db,
        org_id=org.id,
        feature_key="categorize_transactions",
        soft_cap_cents=100,
        hard_cap_cents=500,
        period="monthly",
        session_factory=session_factory,
        **_ACTOR,
    )
    ok = await ai_caps_service.delete_feature_caps(
        db,
        org_id=org.id,
        feature_key="categorize_transactions",
        session_factory=session_factory,
        **_ACTOR,
    )
    assert ok is True
