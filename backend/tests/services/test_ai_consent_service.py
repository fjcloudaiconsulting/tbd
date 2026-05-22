"""Consent service tests (PR1).

Pins:

- append-only semantics — granting a new consent writes a new row,
  never updates the previous one.
- the latest row by ``consented_at`` is what get_current_consents reads.
- a revoked row flips the effective snapshot to all-false even when
  earlier rows had ``allow_*=True``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.org_ai_consent import OrgAIConsent
from app.models.user import Organization, Role, User
from app.services import ai_consent_service


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
async def org_and_user(db: AsyncSession):
    org = Organization(name="Acme", billing_cycle_day=1)
    db.add(org)
    await db.commit()
    user = User(
        org_id=org.id,
        username="owner",
        email="owner@acme.test",
        password_hash="x",
        role=Role.OWNER,
    )
    db.add(user)
    await db.commit()
    return org, user


_ACTOR = {
    "actor_user_id": 1,
    "actor_email": "actor@example.test",
    "request_id": "rid",
    "ip_address": "127.0.0.1",
}


async def test_default_snapshot_is_all_false(db, org_and_user):
    org, _user = org_and_user
    snapshot = await ai_consent_service.get_current_consents(
        db, org_id=org.id
    )
    assert snapshot.has_consent is False
    assert snapshot.allow_training is False
    assert snapshot.allow_rag is False
    assert snapshot.allow_telemetry is False
    assert snapshot.consent_version is None


async def test_grant_then_snapshot_reflects_latest(
    db, session_factory, org_and_user
):
    org, user = org_and_user
    await ai_consent_service.write_consent_row(
        db,
        org_id=org.id,
        consent_version="ai-tos-2026-05-22",
        allow_training=True,
        allow_rag=True,
        allow_telemetry=True,
        revoked=False,
        consented_by_user_id=user.id,
        session_factory=session_factory,
        **_ACTOR,
    )
    snap = await ai_consent_service.get_current_consents(db, org_id=org.id)
    assert snap.has_consent is True
    assert snap.allow_training is True
    assert snap.allow_rag is True
    assert snap.allow_telemetry is True
    assert snap.consent_version == "ai-tos-2026-05-22"


async def test_append_only_two_grants_produce_two_rows(
    db, session_factory, org_and_user
):
    org, user = org_and_user
    await ai_consent_service.write_consent_row(
        db,
        org_id=org.id,
        consent_version="ai-tos-2026-05-22",
        allow_training=False,
        allow_rag=False,
        allow_telemetry=True,
        revoked=False,
        consented_by_user_id=user.id,
        session_factory=session_factory,
        **_ACTOR,
    )
    await ai_consent_service.write_consent_row(
        db,
        org_id=org.id,
        consent_version="ai-tos-2026-05-22",
        allow_training=True,
        allow_rag=True,
        allow_telemetry=True,
        revoked=False,
        consented_by_user_id=user.id,
        session_factory=session_factory,
        **_ACTOR,
    )
    res = await db.execute(
        select(OrgAIConsent).where(OrgAIConsent.org_id == org.id)
    )
    rows = list(res.scalars().all())
    assert len(rows) == 2
    # Latest is the one we last wrote.
    snap = await ai_consent_service.get_current_consents(db, org_id=org.id)
    assert snap.allow_training is True


async def test_revoked_row_flips_snapshot_to_no_consent(
    db, session_factory, org_and_user
):
    org, user = org_and_user
    await ai_consent_service.write_consent_row(
        db,
        org_id=org.id,
        consent_version="ai-tos-2026-05-22",
        allow_training=True,
        allow_rag=True,
        allow_telemetry=True,
        revoked=False,
        consented_by_user_id=user.id,
        session_factory=session_factory,
        **_ACTOR,
    )
    # Now revoke.
    await ai_consent_service.write_consent_row(
        db,
        org_id=org.id,
        consent_version="ai-tos-2026-05-22",
        allow_training=False,
        allow_rag=False,
        allow_telemetry=False,
        revoked=True,
        consented_by_user_id=user.id,
        session_factory=session_factory,
        **_ACTOR,
    )
    snap = await ai_consent_service.get_current_consents(db, org_id=org.id)
    assert snap.has_consent is False
    assert snap.allow_training is False
