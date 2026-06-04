"""Period-bucketing semantics for ``transaction_service.list_transactions``.

The list endpoint must place each row in the period determined by
``settled_date`` (when present) and fall back to ``date`` only when
``settled_date IS NULL``. Sort order follows the same effective-date.

Regression: a pending transaction with ``settled_date=2026-06-05`` was
incorrectly returned by a May 2026 period filter because the query was
filtering on ``Transaction.date`` instead of the effective period date.
"""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.services import transaction_service


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


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    """Minimal org/account/category set so we can write Transaction rows."""
    org = Organization(name="Test", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db_session.add(at)
    await db_session.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db_session.add(acct)
    cat = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE, is_system=False,
    )
    db_session.add(cat)
    await db_session.flush()
    return {"org": org, "account": acct, "category": cat}


def _make_tx(world, *, dt: date, settled_date: date | None, status: TransactionStatus,
             amount: str = "10.00", description: str = "tx") -> Transaction:
    return Transaction(
        org_id=world["org"].id,
        account_id=world["account"].id,
        category_id=world["category"].id,
        description=description,
        amount=Decimal(amount),
        type=TransactionType.EXPENSE,
        status=status,
        date=dt,
        settled_date=settled_date,
    )


# May 2026 period window (closed on both ends, inclusive)
MAY_FROM = date(2026, 5, 1)
MAY_TO = date(2026, 5, 31)


# ── Test cases ────────────────────────────────────────────────────────────────

async def test_settled_in_period_settled_date_inside(db_session, world):
    """SETTLED, date outside period, settled_date inside period -> INCLUDED."""
    tx = _make_tx(
        world, dt=date(2026, 4, 28), settled_date=date(2026, 5, 2),
        status=TransactionStatus.SETTLED, description="settled-in",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert [r.description for r in rows] == ["settled-in"]


async def test_settled_in_period_settled_date_outside(db_session, world):
    """SETTLED, date inside period, settled_date outside -> EXCLUDED.

    Regression guard for the inverse mistake of using `date` for the filter.
    """
    tx = _make_tx(
        world, dt=date(2026, 5, 15), settled_date=date(2026, 6, 2),
        status=TransactionStatus.SETTLED, description="settled-out",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert rows == []


async def test_pending_settling_in_period(db_session, world):
    """PENDING, date before period, settled_date inside -> INCLUDED."""
    tx = _make_tx(
        world, dt=date(2026, 4, 25), settled_date=date(2026, 5, 10),
        status=TransactionStatus.PENDING, description="pending-in",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert [r.description for r in rows] == ["pending-in"]


async def test_pending_settling_after_period(db_session, world):
    """PENDING, date inside period, settled_date after period -> EXCLUDED.

    The user-reported bug: settled_date=2026-06-05 was showing in May filter.
    """
    tx = _make_tx(
        world, dt=date(2026, 5, 20), settled_date=date(2026, 6, 5),
        status=TransactionStatus.PENDING, description="pending-after",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert rows == []


async def test_pending_settling_before_period(db_session, world):
    """PENDING, date inside period, settled_date before period start -> EXCLUDED."""
    tx = _make_tx(
        world, dt=date(2026, 5, 20), settled_date=date(2026, 4, 28),
        status=TransactionStatus.PENDING, description="pending-before",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert rows == []


async def test_pending_no_settled_date_falls_back_to_date(db_session, world):
    """PENDING, settled_date IS NULL, date inside -> INCLUDED.

    Hand-keyed pending entries without an estimate fall back to ``date``.
    """
    tx = _make_tx(
        world, dt=date(2026, 5, 12), settled_date=None,
        status=TransactionStatus.PENDING, description="pending-null",
    )
    db_session.add(tx)
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    assert [r.description for r in rows] == ["pending-null"]


# ``test_settled_no_settled_date_defensive_fallback`` was removed when the
# SETTLED-implies-settled_date invariant was added (migration 036): a SETTLED
# row with settled_date=NULL is no longer a state the database accepts, so the
# COALESCE fallback for that case is now unreachable by construction.
# The COALESCE in ``effective_period_date_expr()`` still serves PENDING rows
# without an estimate, exercised by ``test_pending_no_settled_date_falls_back_to_date``.


async def test_sort_order_uses_effective_date(db_session, world):
    """Mixed pending+settled rows sort by COALESCE(settled_date, date) DESC, id DESC.

    Construct rows whose ``date`` order disagrees with their effective-date
    order so the assertion fails if the ORDER BY still uses ``Transaction.date``.
    """
    # tx_a: settled, date=05-15, settled_date=05-15  -> effective 05-15
    tx_a = _make_tx(
        world, dt=date(2026, 5, 15), settled_date=date(2026, 5, 15),
        status=TransactionStatus.SETTLED, description="A",
    )
    # tx_b: pending, date=05-05, settled_date=05-25  -> effective 05-25
    tx_b = _make_tx(
        world, dt=date(2026, 5, 5), settled_date=date(2026, 5, 25),
        status=TransactionStatus.PENDING, description="B",
    )
    # tx_c: settled, date=05-20, settled_date=05-10  -> effective 05-10
    tx_c = _make_tx(
        world, dt=date(2026, 5, 20), settled_date=date(2026, 5, 10),
        status=TransactionStatus.SETTLED, description="C",
    )
    db_session.add_all([tx_a, tx_b, tx_c])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, date_from=MAY_FROM, date_to=MAY_TO,
    )
    # Effective dates: B=05-25, A=05-15, C=05-10  ->  DESC: B, A, C
    assert [r.description for r in rows] == ["B", "A", "C"]
