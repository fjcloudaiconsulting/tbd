"""Category filter regression coverage for ``transaction_service.list_transactions``.

The /transactions page sends ``category_id=<id>`` on the query string when the
user picks a category from the dropdown. The backend service must restrict the
result to rows whose ``category_id`` matches. A 2026-05-13 user report flagged
this filter as a no-op, so this test pins the contract.
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
    """Org + account + two top-level categories so we can prove the
    filter discriminates. A subcategory of ``food`` is also created
    so we can pin the "selecting a master includes its subs" contract.
    """
    org = Organization(name="Test", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True,
    )
    db_session.add(at)
    await db_session.flush()
    acct = Account(
        org_id=org.id, name="Main", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db_session.add(acct)
    groceries = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE, is_system=False,
    )
    dining = Category(
        org_id=org.id, name="Dining", slug="dining",
        type=CategoryType.EXPENSE, is_system=False,
    )
    db_session.add_all([groceries, dining])
    await db_session.flush()
    # Subcategory of ``groceries`` (master), used in
    # ``test_master_category_filter_includes_subcategories``.
    groceries_sub = Category(
        org_id=org.id, name="Bulk", slug="bulk",
        type=CategoryType.EXPENSE, is_system=False,
        parent_id=groceries.id,
    )
    db_session.add(groceries_sub)
    await db_session.flush()
    return {
        "org": org, "account": acct,
        "groceries": groceries, "dining": dining,
        "groceries_sub": groceries_sub,
    }


def _make_tx(
    world,
    *,
    category: Category,
    description: str,
    dt: date = date(2026, 5, 15),
    amount: str = "10.00",
) -> Transaction:
    return Transaction(
        org_id=world["org"].id,
        account_id=world["account"].id,
        category_id=category.id,
        description=description,
        amount=Decimal(amount),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=dt,
        settled_date=dt,
    )


async def test_category_filter_returns_only_matching_rows(db_session, world):
    """Passing ``category_id`` must restrict the result to that category."""
    db_session.add_all([
        _make_tx(world, category=world["groceries"], description="g1"),
        _make_tx(world, category=world["groceries"], description="g2"),
        _make_tx(world, category=world["dining"], description="d1"),
        _make_tx(world, category=world["dining"], description="d2"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, category_id=world["groceries"].id,
    )
    assert sorted(r.description for r in rows) == ["g1", "g2"]


async def test_category_filter_none_returns_all(db_session, world):
    """``category_id=None`` (the default) must not filter."""
    db_session.add_all([
        _make_tx(world, category=world["groceries"], description="g1"),
        _make_tx(world, category=world["dining"], description="d1"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id,
    )
    assert sorted(r.description for r in rows) == ["d1", "g1"]


async def test_master_category_filter_includes_subcategories(db_session, world):
    """Selecting a master category must also return rows tagged with any
    of its subcategories.

    Reproduces the 2026-05-13 user report: the dropdown lists masters and
    subs flat, so a user picking the "Food & Dining" master expects every
    row in that food bucket, including ones tagged with "Groceries" /
    "Restaurants" / etc. Exact-match against ``category_id`` excluded them.
    """
    db_session.add_all([
        _make_tx(world, category=world["groceries"], description="g-master"),
        _make_tx(world, category=world["groceries_sub"], description="g-sub"),
        _make_tx(world, category=world["dining"], description="d-master"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, category_id=world["groceries"].id,
    )
    assert sorted(r.description for r in rows) == ["g-master", "g-sub"]


async def test_subcategory_filter_exact_match_only(db_session, world):
    """Selecting a subcategory must NOT pull in its master's other subs
    or rows tagged directly with the master.

    The master-inclusion behavior is one-directional: master -> subs, not
    the reverse. A user picking "Groceries" sub wants only Groceries rows.
    """
    db_session.add_all([
        _make_tx(world, category=world["groceries"], description="g-master"),
        _make_tx(world, category=world["groceries_sub"], description="g-sub"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, category_id=world["groceries_sub"].id,
    )
    assert [r.description for r in rows] == ["g-sub"]


async def test_category_filter_compounds_with_date_range(db_session, world):
    """``category_id`` AND ``date_from``/``date_to`` must both apply."""
    db_session.add_all([
        _make_tx(
            world, category=world["groceries"], description="g-may",
            dt=date(2026, 5, 15),
        ),
        _make_tx(
            world, category=world["groceries"], description="g-apr",
            dt=date(2026, 4, 15),
        ),
        _make_tx(
            world, category=world["dining"], description="d-may",
            dt=date(2026, 5, 15),
        ),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id,
        category_id=world["groceries"].id,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    assert [r.description for r in rows] == ["g-may"]
