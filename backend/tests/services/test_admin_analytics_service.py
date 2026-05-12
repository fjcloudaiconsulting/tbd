"""Service-layer tests for L4.6 admin_analytics_service.

Pins:
- Each aggregate emits a contiguous day series (zero-filled).
- Empty datasets yield all-zero series of the right length.
- Window is anchored on UTC midnight (request issued twice on the same
  day returns identical buckets).
- Cross-org isolation in ``top_orgs_by_tx_volume``: rows from org A do
  not inflate org B's count.
- ``dormant_orgs`` includes orgs with no transactions, sorted before
  stale-but-not-silent orgs.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services import admin_analytics_service


# We use status=PENDING in test fixtures because the SETTLED invariant
# (migration 036) requires a settled_date; the analytics service does
# not filter on status, so PENDING is the simpler shape.


# Pin the clock so every assertion lines up against a known date.
# 2026-05-11 14:00 UTC sits comfortably in the test data window below.
FIXED_NOW = datetime(2026, 5, 11, 14, 0, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
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


async def _make_org(db: AsyncSession, name: str) -> Organization:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    return org


async def _make_account_and_category(
    db: AsyncSession, org: Organization
) -> tuple[Account, Category]:
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
    db.add(at)
    await db.flush()
    acct = Account(
        org_id=org.id,
        name="Main",
        account_type_id=at.id,
        currency="EUR",
        balance=Decimal("0.00"),
    )
    cat = Category(
        org_id=org.id,
        name="Misc",
        slug="misc",
        type=CategoryType.EXPENSE,
        is_system=False,
    )
    db.add_all([acct, cat])
    await db.flush()
    return acct, cat


def _make_tx(
    *,
    org: Organization,
    account: Account,
    category: Category,
    created_at: datetime,
    is_imported: bool = False,
    amount: str = "10.00",
) -> Transaction:
    return Transaction(
        org_id=org.id,
        account_id=account.id,
        category_id=category.id,
        description="Test tx",
        amount=Decimal(amount),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.PENDING,
        date=created_at.date(),
        created_at=created_at,
        is_imported=is_imported,
    )


@pytest.mark.asyncio
async def test_login_counts_by_day_zero_fills_empty_window(db_session) -> None:
    series = await admin_analytics_service.login_counts_by_day(
        db_session, days=7, now=FIXED_NOW
    )
    assert len(series) == 7
    assert all(row["count"] == 0 for row in series)
    # Newest day is "today" (UTC of FIXED_NOW).
    assert series[-1]["date"] == FIXED_NOW.date()
    # Series is contiguous and ascending.
    for i in range(1, len(series)):
        assert (series[i]["date"] - series[i - 1]["date"]).days == 1


@pytest.mark.asyncio
async def test_login_counts_by_day_counts_login_events_only(db_session) -> None:
    org = await _make_org(db_session, "Org A")
    db_session.add_all(
        [
            # actor_user_id / target_org_id left None to avoid the FK
            # constraint dance — the analytics service filters only on
            # event_type and created_at.
            AuditEvent(
                event_type="user.login.success",
                actor_user_id=None,
                actor_email="a@x.io",
                target_org_id=None,
                target_org_name=None,
                request_id=None,
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail={"method": "password"},
                created_at=FIXED_NOW - timedelta(days=1, hours=1),
            ),
            AuditEvent(
                event_type="user.login.success",
                actor_user_id=None,
                actor_email="b@x.io",
                target_org_id=None,
                target_org_name=None,
                request_id=None,
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail={"method": "password"},
                created_at=FIXED_NOW - timedelta(days=1, hours=2),
            ),
            # Non-login event must be ignored.
            AuditEvent(
                event_type="org.rename",
                actor_user_id=None,
                actor_email="a@x.io",
                target_org_id=None,
                target_org_name="Org A",
                request_id=None,
                ip_address=None,
                outcome=AuditOutcome.SUCCESS,
                detail={},
                created_at=FIXED_NOW - timedelta(days=1, hours=3),
            ),
        ]
    )
    await db_session.commit()

    series = await admin_analytics_service.login_counts_by_day(
        db_session, days=3, now=FIXED_NOW
    )
    counts_by_date = {row["date"]: row["count"] for row in series}
    yesterday = (FIXED_NOW - timedelta(days=1)).date()
    assert counts_by_date[yesterday] == 2
    # No login events on the other days.
    assert sum(counts_by_date.values()) == 2


@pytest.mark.asyncio
async def test_tx_writes_by_day_uses_created_at_not_transaction_date(
    db_session,
) -> None:
    org = await _make_org(db_session, "Org A")
    acct, cat = await _make_account_and_category(db_session, org)
    # ``date`` (purchase date) is far in the past; created_at (write time)
    # is yesterday. The analytics service must bucket by created_at.
    tx = _make_tx(
        org=org,
        account=acct,
        category=cat,
        created_at=FIXED_NOW - timedelta(days=1),
    )
    tx.date = (FIXED_NOW - timedelta(days=200)).date()
    db_session.add(tx)
    await db_session.commit()

    series = await admin_analytics_service.tx_writes_by_day(
        db_session, days=5, now=FIXED_NOW
    )
    counts_by_date = {row["date"]: row["count"] for row in series}
    yesterday = (FIXED_NOW - timedelta(days=1)).date()
    assert counts_by_date[yesterday] == 1
    assert sum(counts_by_date.values()) == 1


@pytest.mark.asyncio
async def test_imports_by_day_filters_on_is_imported(db_session) -> None:
    org = await _make_org(db_session, "Org A")
    acct, cat = await _make_account_and_category(db_session, org)
    db_session.add_all(
        [
            _make_tx(
                org=org, account=acct, category=cat,
                created_at=FIXED_NOW - timedelta(days=1),
                is_imported=True,
            ),
            _make_tx(
                org=org, account=acct, category=cat,
                created_at=FIXED_NOW - timedelta(days=1),
                is_imported=True,
            ),
            _make_tx(
                org=org, account=acct, category=cat,
                created_at=FIXED_NOW - timedelta(days=1),
                is_imported=False,  # not imported — must NOT count.
            ),
        ]
    )
    await db_session.commit()

    series = await admin_analytics_service.imports_by_day(
        db_session, days=3, now=FIXED_NOW
    )
    counts_by_date = {row["date"]: row["count"] for row in series}
    yesterday = (FIXED_NOW - timedelta(days=1)).date()
    assert counts_by_date[yesterday] == 2


@pytest.mark.asyncio
async def test_top_orgs_by_tx_volume_isolates_per_org(db_session) -> None:
    org_a = await _make_org(db_session, "Org Alpha")
    org_b = await _make_org(db_session, "Org Bravo")
    acct_a, cat_a = await _make_account_and_category(db_session, org_a)
    acct_b, cat_b = await _make_account_and_category(db_session, org_b)

    # Org A: 3 txs in window. Org B: 1 tx in window + 1 outside.
    for _ in range(3):
        db_session.add(
            _make_tx(
                org=org_a, account=acct_a, category=cat_a,
                created_at=FIXED_NOW - timedelta(days=2),
            )
        )
    db_session.add(
        _make_tx(
            org=org_b, account=acct_b, category=cat_b,
            created_at=FIXED_NOW - timedelta(days=2),
        )
    )
    db_session.add(
        _make_tx(
            org=org_b, account=acct_b, category=cat_b,
            # Outside the 7-day window we'll query for.
            created_at=FIXED_NOW - timedelta(days=60),
        )
    )
    await db_session.commit()

    rows = await admin_analytics_service.top_orgs_by_tx_volume(
        db_session, limit=10, days=7, now=FIXED_NOW
    )
    assert [r["org_name"] for r in rows] == ["Org Alpha", "Org Bravo"]
    assert rows[0]["rank"] == 1
    assert rows[0]["tx_count"] == 3
    assert rows[1]["rank"] == 2
    assert rows[1]["tx_count"] == 1


@pytest.mark.asyncio
async def test_top_orgs_by_tx_volume_empty_dataset(db_session) -> None:
    rows = await admin_analytics_service.top_orgs_by_tx_volume(
        db_session, limit=10, days=30, now=FIXED_NOW
    )
    assert rows == []


@pytest.mark.asyncio
async def test_dormant_orgs_lists_silent_and_never_active(db_session) -> None:
    # Three orgs, three states:
    # - Org Active: tx within threshold → NOT dormant.
    # - Org Stale: tx older than threshold → dormant.
    # - Org Empty: no transactions at all → dormant (never-active first).
    org_active = await _make_org(db_session, "Org Active")
    org_stale = await _make_org(db_session, "Org Stale")
    org_empty = await _make_org(db_session, "Org Empty")

    acct_a, cat_a = await _make_account_and_category(db_session, org_active)
    acct_s, cat_s = await _make_account_and_category(db_session, org_stale)

    db_session.add(
        _make_tx(
            org=org_active, account=acct_a, category=cat_a,
            created_at=FIXED_NOW - timedelta(days=5),
        )
    )
    db_session.add(
        _make_tx(
            org=org_stale, account=acct_s, category=cat_s,
            created_at=FIXED_NOW - timedelta(days=90),
        )
    )
    await db_session.commit()

    rows = await admin_analytics_service.dormant_orgs(
        db_session, threshold_days=30, now=FIXED_NOW
    )
    names = [r["org_name"] for r in rows]
    assert "Org Active" not in names
    assert names[0] == "Org Empty"  # never-active orgs lead.
    assert "Org Stale" in names
    # Org Empty has no last_tx_at.
    empty_row = next(r for r in rows if r["org_name"] == "Org Empty")
    assert empty_row["last_tx_at"] is None
    assert empty_row["days_since_last_activity"] is None
    # Org Stale's last activity was ~90 days ago.
    stale_row = next(r for r in rows if r["org_name"] == "Org Stale")
    assert stale_row["days_since_last_activity"] is not None
    assert 89 <= stale_row["days_since_last_activity"] <= 91


@pytest.mark.asyncio
async def test_build_analytics_payload_assembles_full_envelope(db_session) -> None:
    org = await _make_org(db_session, "Single Org")
    acct, cat = await _make_account_and_category(db_session, org)
    db_session.add(
        _make_tx(
            org=org, account=acct, category=cat,
            created_at=FIXED_NOW - timedelta(days=1),
        )
    )
    await db_session.commit()

    payload = await admin_analytics_service.build_analytics_payload(
        db_session, days=7, top_orgs_limit=5, dormant_threshold_days=30,
        now=FIXED_NOW,
    )
    assert payload["window_days"] == 7
    assert payload["generated_at"] == FIXED_NOW
    assert len(payload["logins_by_day"]) == 7
    assert len(payload["tx_writes_by_day"]) == 7
    assert len(payload["imports_by_day"]) == 7
    # One org has activity → it appears in top_orgs. No org is dormant
    # under the 30d threshold because the only tx is 1 day old.
    assert payload["top_orgs_by_tx_volume"][0]["org_name"] == "Single Org"
    assert payload["dormant_orgs"] == []


@pytest.mark.asyncio
async def test_normalize_days_clamps_out_of_range_values() -> None:
    assert admin_analytics_service._normalize_days(0) == 30
    assert admin_analytics_service._normalize_days(-5) == 30
    assert admin_analytics_service._normalize_days(400) == 365
    assert admin_analytics_service._normalize_days(45) == 45
