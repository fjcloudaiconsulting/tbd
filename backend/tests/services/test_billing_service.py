"""Tests for billing_service.close_period — covers the duplicate-stub regression
that PR #93 fixes, plus the race-recovery defensive layer."""
from __future__ import annotations

import asyncio
import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.billing import BillingPeriod
from app.models.user import Organization
from app.services import billing_service
from app.services.exceptions import ValidationError


@pytest_asyncio.fixture
async def session_factory():
    """In-memory SQLite shared across sessions via StaticPool."""
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


async def _seed_org_with_open_period(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: int = 1,
    start: datetime.date | None = None,
) -> datetime.date:
    start = start or (datetime.date.today() - datetime.timedelta(days=10))
    async with factory() as db:
        db.add(Organization(id=org_id, name="test-org", billing_cycle_day=1))
        await db.commit()
        db.add(BillingPeriod(org_id=org_id, start_date=start, end_date=None))
        await db.commit()
    return start


@pytest.mark.asyncio
async def test_close_period_inserts_new_open_period_when_no_stub_exists(
    session_factory,
):
    org_id = 1
    start = await _seed_org_with_open_period(session_factory, org_id=org_id)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id)

    today = datetime.date.today()
    assert result.end_date is None
    assert result.start_date == today

    async with session_factory() as db:
        periods = (
            await db.execute(
                select(BillingPeriod)
                .where(BillingPeriod.org_id == org_id)
                .order_by(BillingPeriod.start_date)
            )
        ).scalars().all()
    assert [p.start_date for p in periods] == [start, today]
    assert periods[0].end_date == today - datetime.timedelta(days=1)
    assert periods[1].end_date is None


@pytest.mark.asyncio
async def test_close_period_revives_existing_stub_at_new_start(session_factory):
    """Reproduces PR #93: a future stub at close_date+1 must be revived,
    not duplicated."""
    org_id = 1
    today = datetime.date.today()
    start = today - datetime.timedelta(days=10)
    await _seed_org_with_open_period(session_factory, org_id=org_id, start=start)

    # Pre-existing stub at exactly close_date+1 (= today by default).
    stub_end = today + datetime.timedelta(days=29)
    async with session_factory() as db:
        db.add(BillingPeriod(org_id=org_id, start_date=today, end_date=stub_end))
        await db.commit()
        stub_id = (
            await db.scalar(
                select(BillingPeriod.id).where(
                    BillingPeriod.org_id == org_id,
                    BillingPeriod.start_date == today,
                )
            )
        )

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id)

    assert result.id == stub_id, "stub should be revived, not duplicated"
    assert result.end_date is None, "revived stub must be open (end_date=None)"
    assert result.start_date == today

    async with session_factory() as db:
        all_periods = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == org_id)
            )
        ).scalars().all()
    assert len(all_periods) == 2, "no duplicate row created"


@pytest.mark.asyncio
async def test_close_period_recovers_from_integrity_error_on_concurrent_insert(
    session_factory, monkeypatch
):
    """Defensive: if a concurrent peer inserts (org_id, new_start) between our
    SELECT and our INSERT, the commit raises IntegrityError. close_period must
    rollback, re-fetch, and revive the winning row instead of returning 500."""
    org_id = 1
    today = datetime.date.today()
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=today - datetime.timedelta(days=10)
    )

    # Simulate the race: a peer has already inserted at today, but our
    # existence-check is patched to return None (as it would if our SELECT ran
    # before the peer's commit was visible). The INSERT then collides.
    async with session_factory() as db:
        db.add(
            BillingPeriod(
                org_id=org_id,
                start_date=today,
                end_date=today + datetime.timedelta(days=15),
            )
        )
        await db.commit()
        peer_id = (
            await db.scalar(
                select(BillingPeriod.id).where(
                    BillingPeriod.org_id == org_id,
                    BillingPeriod.start_date == today,
                )
            )
        )

    real_scalar = AsyncSession.scalar
    call_count = {"n": 0}

    async def patched_scalar(self, statement, *args, **kwargs):
        # The first scalar call inside close_period after get_current_period
        # is the existence-check at new_start. Force it to miss so the code
        # takes the INSERT path and trips IntegrityError.
        call_count["n"] += 1
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        is_existence_check = (
            "billing_periods" in compiled
            and "start_date" in compiled
            and call_count["n"] == 2  # 1st = get_current_period; 2nd = our check
        )
        if is_existence_check:
            return None
        return await real_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", patched_scalar)

    async with session_factory() as db:
        result = await billing_service.close_period(db, org_id)

    assert result.id == peer_id, "must converge on the peer's row, not a new one"
    assert result.end_date is None

    async with session_factory() as db:
        all_periods = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == org_id)
            )
        ).scalars().all()
    assert len(all_periods) == 2, "race recovery must not leave duplicates"
    closed = [p for p in all_periods if p.end_date is not None]
    open_ = [p for p in all_periods if p.end_date is None]
    assert len(closed) == 1 and len(open_) == 1


@pytest.mark.asyncio
async def test_close_period_rejects_close_date_before_period_start(session_factory):
    org_id = 1
    today = datetime.date.today()
    await _seed_org_with_open_period(
        session_factory, org_id=org_id, start=today - datetime.timedelta(days=2)
    )

    with pytest.raises(ValidationError):
        async with session_factory() as db:
            await billing_service.close_period(
                db, org_id, close_date=today - datetime.timedelta(days=10)
            )
