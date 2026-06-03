"""list_transactions returns (items, total) with server-side sort.

- total is the full filtered count, independent of limit/offset
- sort by each whitelisted key (asc + desc), incl. account_name / category_name
- id-desc tiebreaker keeps pagination stable across equal sort values
- invalid sort_by / sort_dir raise ValidationError
- org-scoping: a second org's rows are never counted or returned
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.services import transaction_service
from app.services.exceptions import ValidationError

pytestmark = pytest.mark.asyncio


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


async def _org(db, name):
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    return org.id, at.id


async def _acct(db, org_id, at_id, name):
    a = Account(org_id=org_id, name=name, account_type_id=at_id,
                balance=Decimal("0"), currency="EUR")
    db.add(a)
    await db.flush()
    return a.id


async def _cat(db, org_id, name, slug):
    c = Category(org_id=org_id, name=name, slug=slug, type=CategoryType.EXPENSE)
    db.add(c)
    await db.flush()
    return c.id


async def _tx(db, org_id, acct_id, cat_id, *, desc, amount, when, status=TransactionStatus.SETTLED):
    t = Transaction(
        org_id=org_id, account_id=acct_id, category_id=cat_id, description=desc,
        amount=Decimal(amount), type=TransactionType.EXPENSE, status=status,
        date=when, settled_date=when if status == TransactionStatus.SETTLED else None,
    )
    db.add(t)
    await db.flush()
    return t.id


async def test_envelope_total_independent_of_limit(db_session):
    org_id, at = await _org(db_session, "A")
    acct = await _acct(db_session, org_id, at, "Main")
    cat = await _cat(db_session, org_id, "Gym", "gym")
    for i in range(5):
        await _tx(db_session, org_id, acct, cat, desc=f"t{i}", amount="10.00",
                  when=date.today() - timedelta(days=i))
    await db_session.commit()

    items, total = await transaction_service.list_transactions(db_session, org_id, limit=2, offset=0)
    assert total == 5
    assert len(items) == 2


async def test_sort_amount_asc_and_desc(db_session):
    org_id, at = await _org(db_session, "A")
    acct = await _acct(db_session, org_id, at, "Main")
    cat = await _cat(db_session, org_id, "Gym", "gym")
    await _tx(db_session, org_id, acct, cat, desc="cheap", amount="5.00", when=date.today())
    await _tx(db_session, org_id, acct, cat, desc="pricey", amount="50.00", when=date.today())
    await db_session.commit()

    asc, _ = await transaction_service.list_transactions(db_session, org_id, sort_by="amount", sort_dir="asc")
    assert [t.description for t in asc] == ["cheap", "pricey"]
    desc, _ = await transaction_service.list_transactions(db_session, org_id, sort_by="amount", sort_dir="desc")
    assert [t.description for t in desc] == ["pricey", "cheap"]


async def test_sort_account_name(db_session):
    org_id, at = await _org(db_session, "A")
    a1 = await _acct(db_session, org_id, at, "Zebra")
    a2 = await _acct(db_session, org_id, at, "Alpha")
    cat = await _cat(db_session, org_id, "Gym", "gym")
    await _tx(db_session, org_id, a1, cat, desc="z", amount="1.00", when=date.today())
    await _tx(db_session, org_id, a2, cat, desc="a", amount="1.00", when=date.today())
    await db_session.commit()

    items, _ = await transaction_service.list_transactions(db_session, org_id, sort_by="account_name", sort_dir="asc")
    assert [t.description for t in items] == ["a", "z"]


async def test_tiebreaker_stable_across_pages(db_session):
    org_id, at = await _org(db_session, "A")
    acct = await _acct(db_session, org_id, at, "Main")
    cat = await _cat(db_session, org_id, "Gym", "gym")
    same = date.today()
    ids = [await _tx(db_session, org_id, acct, cat, desc=f"t{i}", amount="10.00", when=same) for i in range(4)]
    await db_session.commit()

    page1, total = await transaction_service.list_transactions(db_session, org_id, sort_by="date", sort_dir="desc", limit=2, offset=0)
    page2, _ = await transaction_service.list_transactions(db_session, org_id, sort_by="date", sort_dir="desc", limit=2, offset=2)
    seen = [t.id for t in page1] + [t.id for t in page2]
    assert sorted(seen) == sorted(ids)
    assert len(set(seen)) == 4


async def test_invalid_sort_raises(db_session):
    org_id, _ = await _org(db_session, "A")
    with pytest.raises(ValidationError):
        await transaction_service.list_transactions(db_session, org_id, sort_by="evil_column")
    with pytest.raises(ValidationError):
        await transaction_service.list_transactions(db_session, org_id, sort_dir="sideways")


async def test_org_scoping(db_session):
    org_a, at_a = await _org(db_session, "A")
    org_b, at_b = await _org(db_session, "B")
    aa = await _acct(db_session, org_a, at_a, "MainA")
    ab = await _acct(db_session, org_b, at_b, "MainB")
    ca = await _cat(db_session, org_a, "Gym", "gym")
    cb = await _cat(db_session, org_b, "Gym", "gym")
    await _tx(db_session, org_a, aa, ca, desc="a", amount="1.00", when=date.today())
    await _tx(db_session, org_b, ab, cb, desc="b1", amount="1.00", when=date.today())
    await _tx(db_session, org_b, ab, cb, desc="b2", amount="1.00", when=date.today())
    await db_session.commit()

    items, total = await transaction_service.list_transactions(db_session, org_a)
    assert total == 1
    assert [t.description for t in items] == ["a"]


async def test_sort_category_name(db_session):
    org_id, at = await _org(db_session, "A")
    acct = await _acct(db_session, org_id, at, "Main")
    c1 = await _cat(db_session, org_id, "Zoo", "zoo")
    c2 = await _cat(db_session, org_id, "Auto", "auto")
    await _tx(db_session, org_id, acct, c1, desc="z", amount="1.00", when=date.today())
    await _tx(db_session, org_id, acct, c2, desc="a", amount="1.00", when=date.today())
    await db_session.commit()

    items, _ = await transaction_service.list_transactions(
        db_session, org_id, sort_by="category_name", sort_dir="asc"
    )
    assert [t.description for t in items] == ["a", "z"]


async def test_filter_composes_with_sort(db_session):
    org_id, at = await _org(db_session, "A")
    acct = await _acct(db_session, org_id, at, "Main")
    keep = await _cat(db_session, org_id, "Keep", "keep")
    drop = await _cat(db_session, org_id, "Drop", "drop")
    await _tx(db_session, org_id, acct, keep, desc="k-big", amount="50.00", when=date.today())
    await _tx(db_session, org_id, acct, keep, desc="k-small", amount="5.00", when=date.today())
    await _tx(db_session, org_id, acct, drop, desc="d", amount="99.00", when=date.today())
    await db_session.commit()

    items, total = await transaction_service.list_transactions(
        db_session, org_id, category_id=keep, sort_by="amount", sort_dir="asc"
    )
    assert total == 2  # filter applied to count too
    assert [t.description for t in items] == ["k-small", "k-big"]  # sort within filtered set
