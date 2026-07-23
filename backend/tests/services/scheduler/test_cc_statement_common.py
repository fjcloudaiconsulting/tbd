"""Tests for shared CC-alert helpers (Task 7, CC Statement Alerts V1).

Covers:
  - ``active_cc_accounts``: org-scoped query for active credit_card accounts
    with a non-null ``close_day`` (the CC-only cycle-bearing column).
  - ``most_recent_closed_cycle``: resolves the most recently CLOSED cycle
    for a CC account as of ``today``, anchoring the "gap" re-resolution on
    ``cyc.period_start - 1 day`` (never ``today - 1 day``), plus the
    backfill guard (design C1) that suppresses cycles that closed on/before
    the account's creation date.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.account import Account, AccountType
from app.models.base import Base
from app.models.user import Organization
from app.services.scheduler.jobs import cc_statement_common as c


# ── most_recent_closed_cycle (pure function, no DB) ─────────────────────────


def _cc(close_day=20, created=date(2020, 1, 1)):
    return SimpleNamespace(
        close_day=close_day,
        payment_day=None,
        payment_day_relative_month=None,
        created_at=SimpleNamespace(date=lambda: created),
    )


def test_on_close_day_is_that_cycle():
    cyc = c.most_recent_closed_cycle(_cc(20), date(2026, 7, 20))
    assert cyc.period_end_inclusive == date(2026, 7, 20)


def test_twenty_days_past_close_resolves_prior_cycle():
    cyc = c.most_recent_closed_cycle(_cc(20), date(2026, 8, 9))  # 20 days past Jul 20 close
    assert cyc.period_end_inclusive == date(2026, 7, 20)


def test_backfill_guard_suppresses_precreation_cycle():
    # card created 2026-07-23, close_day 20 → most recent close 2026-07-20 predates creation.
    assert c.most_recent_closed_cycle(_cc(20, created=date(2026, 7, 23)), date(2026, 7, 24)) is None


def test_backfill_guard_allows_cycle_that_closed_after_creation():
    # card created 2026-06-25 (before the 2026-07-20 close) → cycle is real, not backfill noise.
    cyc = c.most_recent_closed_cycle(_cc(20, created=date(2026, 6, 25)), date(2026, 7, 20))
    assert cyc is not None
    assert cyc.period_end_inclusive == date(2026, 7, 20)


# ── active_cc_accounts (org-scoped DB query) ────────────────────────────────


@pytest_asyncio.fixture
async def db_session():
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_two_orgs(db: AsyncSession) -> dict:
    """Seed two orgs, each with a mix of CC + non-CC accounts, and one org
    with an inactive CC + a CC with no close_day set, to exercise every
    filter leg (org_id, slug==credit_card, is_active, close_day IS NOT NULL).
    """
    org_a = Organization(name="Org A", billing_cycle_day=1)
    org_b = Organization(name="Org B", billing_cycle_day=1)
    db.add_all([org_a, org_b])
    await db.flush()

    cc_a = AccountType(org_id=org_a.id, name="Credit Card", slug="credit_card", is_system=True)
    checking_a = AccountType(org_id=org_a.id, name="Checking", slug="checking", is_system=True)
    cc_b = AccountType(org_id=org_b.id, name="Credit Card", slug="credit_card", is_system=True)
    db.add_all([cc_a, checking_a, cc_b])
    await db.flush()

    active_cc_a = Account(
        org_id=org_a.id, name="Active CC A", account_type_id=cc_a.id,
        balance=Decimal("0.00"), currency="EUR", is_active=True, close_day=20,
    )
    inactive_cc_a = Account(
        org_id=org_a.id, name="Inactive CC A", account_type_id=cc_a.id,
        balance=Decimal("0.00"), currency="EUR", is_active=False, close_day=20,
    )
    cc_no_close_day_a = Account(
        org_id=org_a.id, name="CC No Close Day A", account_type_id=cc_a.id,
        balance=Decimal("0.00"), currency="EUR", is_active=True, close_day=None,
    )
    checking_account_a = Account(
        org_id=org_a.id, name="Checking A", account_type_id=checking_a.id,
        balance=Decimal("0.00"), currency="EUR", is_active=True,
    )
    active_cc_b = Account(
        org_id=org_b.id, name="Active CC B", account_type_id=cc_b.id,
        balance=Decimal("0.00"), currency="EUR", is_active=True, close_day=15,
    )
    db.add_all(
        [active_cc_a, inactive_cc_a, cc_no_close_day_a, checking_account_a, active_cc_b]
    )
    await db.flush()
    await db.commit()

    return {
        "org_a_id": org_a.id,
        "org_b_id": org_b.id,
        "active_cc_a_id": active_cc_a.id,
        "active_cc_b_id": active_cc_b.id,
    }


@pytest.mark.asyncio
async def test_active_cc_accounts_returns_only_org_scoped_active_cc_rows(db_session):
    seed = await _seed_two_orgs(db_session)

    result = await c.active_cc_accounts(db_session, seed["org_a_id"])

    assert [a.id for a in result] == [seed["active_cc_a_id"]]


@pytest.mark.asyncio
async def test_active_cc_accounts_is_org_isolated(db_session):
    seed = await _seed_two_orgs(db_session)

    result_b = await c.active_cc_accounts(db_session, seed["org_b_id"])

    assert [a.id for a in result_b] == [seed["active_cc_b_id"]]
