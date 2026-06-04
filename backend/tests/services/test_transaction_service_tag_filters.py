"""Tag-filter semantics for ``transaction_service.list_transactions``.

PR-Tags-A contract additions:

- Transaction list/detail responses include ``tags: list[TagResponse]``.
- The list endpoint accepts ``tags``, ``tags_exclude``, and
  ``tag_match`` filters with AND semantics by default and OR when
  ``tag_match='any'``.

Tests cover the service layer directly (the router-layer wiring is
exercised in ``tests/routers/test_transactions_tag_filters.py``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.category import CategoryType
from app.models.tag import Tag, TransactionTag
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
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def world(db_session: AsyncSession):
    org = Organization(name="Test", billing_cycle_day=1)
    db_session.add(org)
    await db_session.flush()
    at = AccountType(
        org_id=org.id, name="Checking", slug="checking", is_system=True
    )
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


def _tx(world, *, description: str, dt: date = date(2026, 5, 1)) -> Transaction:
    return Transaction(
        org_id=world["org"].id,
        account_id=world["account"].id,
        category_id=world["category"].id,
        description=description,
        amount=Decimal("10.00"),
        type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED,
        date=dt,
        settled_date=dt,
    )


async def _attach_tags(db, tx: Transaction, names: list[str], org_id: int):
    """Create org-local tags (if missing) and link them to the
    transaction. Tests reuse this for setup."""
    for n in names:
        tag = Tag(org_id=org_id, name=n, name_normalized=n.lower())
        db.add(tag)
        await db.flush()
        db.add(TransactionTag(transaction_id=tx.id, tag_id=tag.id))


# ---------------------------------------------------------------------------
# Embedded tags on response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_empty_tag_list_for_untagged_transaction(
    db_session, world
):
    """A transaction with no tags must serialize ``tags: []``."""
    tx = _tx(world, description="no tags")
    db_session.add(tx)
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id
    )
    assert len(txns) == 1
    response = transaction_service.to_response(txns[0])
    assert response.tags == []


@pytest.mark.asyncio
async def test_list_returns_attached_tags(db_session, world):
    """When a transaction has tags, the response includes them.

    The response shape must carry ``id``, ``name`` and ``name_normalized``
    per ``TagResponse``. The order is by ``name_normalized`` ascending
    (matches the ``Tag.tags`` relationship's ``order_by``).
    """
    tx = _tx(world, description="tagged")
    db_session.add(tx)
    await db_session.flush()
    await _attach_tags(
        db_session, tx, ["Insurance", "monthly"], world["org"].id
    )
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id
    )
    response = transaction_service.to_response(txns[0])
    names = [t.name_normalized for t in response.tags]
    assert names == ["insurance", "monthly"]


# ---------------------------------------------------------------------------
# Filter: tags (default AND)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_tags_single_returns_only_tagged_rows(db_session, world):
    """``tags=insurance`` returns only transactions tagged "insurance"."""
    tx_a = _tx(world, description="a", dt=date(2026, 5, 1))
    tx_b = _tx(world, description="b", dt=date(2026, 5, 2))
    tx_c = _tx(world, description="c", dt=date(2026, 5, 3))
    db_session.add_all([tx_a, tx_b, tx_c])
    await db_session.flush()
    await _attach_tags(db_session, tx_a, ["insurance"], world["org"].id)
    await _attach_tags(db_session, tx_b, ["vacation"], world["org"].id)
    # tx_c has no tags
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, tags=["insurance"]
    )
    assert [tx.id for tx in txns] == [tx_a.id]


@pytest.mark.asyncio
async def test_filter_tags_default_is_and(db_session, world):
    """``tags=insurance,vacation`` (AND) returns only transactions that
    have BOTH tags. Default ``tag_match='all'``.
    """
    tx_both = _tx(world, description="both", dt=date(2026, 5, 1))
    tx_one = _tx(world, description="one", dt=date(2026, 5, 2))
    tx_other = _tx(world, description="other", dt=date(2026, 5, 3))
    db_session.add_all([tx_both, tx_one, tx_other])
    await db_session.flush()
    # Two distinct Tag rows for two distinct names; reuse them for tx_both.
    insurance = Tag(
        org_id=world["org"].id, name="insurance", name_normalized="insurance"
    )
    vacation = Tag(
        org_id=world["org"].id, name="vacation", name_normalized="vacation"
    )
    db_session.add_all([insurance, vacation])
    await db_session.flush()
    db_session.add_all([
        TransactionTag(transaction_id=tx_both.id, tag_id=insurance.id),
        TransactionTag(transaction_id=tx_both.id, tag_id=vacation.id),
        TransactionTag(transaction_id=tx_one.id, tag_id=insurance.id),
        TransactionTag(transaction_id=tx_other.id, tag_id=vacation.id),
    ])
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, tags=["insurance", "vacation"]
    )
    assert [tx.id for tx in txns] == [tx_both.id]


@pytest.mark.asyncio
async def test_filter_tags_match_any_is_or(db_session, world):
    """``tags=insurance,vacation&tag_match=any`` returns transactions
    tagged with EITHER tag.
    """
    tx_a = _tx(world, description="a", dt=date(2026, 5, 1))
    tx_b = _tx(world, description="b", dt=date(2026, 5, 2))
    tx_c = _tx(world, description="c", dt=date(2026, 5, 3))
    db_session.add_all([tx_a, tx_b, tx_c])
    await db_session.flush()
    await _attach_tags(db_session, tx_a, ["insurance"], world["org"].id)
    await _attach_tags(db_session, tx_b, ["vacation"], world["org"].id)
    # tx_c untagged
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id,
        tags=["insurance", "vacation"], tag_match="any",
    )
    ids = sorted(tx.id for tx in txns)
    assert ids == sorted([tx_a.id, tx_b.id])


# ---------------------------------------------------------------------------
# Filter: tags_exclude
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_tags_exclude(db_session, world):
    """``tags_exclude=insurance`` excludes any transaction tagged
    insurance.
    """
    tx_a = _tx(world, description="a", dt=date(2026, 5, 1))
    tx_b = _tx(world, description="b", dt=date(2026, 5, 2))
    tx_c = _tx(world, description="c", dt=date(2026, 5, 3))
    db_session.add_all([tx_a, tx_b, tx_c])
    await db_session.flush()
    await _attach_tags(db_session, tx_a, ["insurance"], world["org"].id)
    await _attach_tags(db_session, tx_b, ["vacation"], world["org"].id)
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, tags_exclude=["insurance"]
    )
    ids = sorted(tx.id for tx in txns)
    assert ids == sorted([tx_b.id, tx_c.id])


# ---------------------------------------------------------------------------
# Combined filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_tags_combined_with_exclude(db_session, world):
    """Apply ``tags=vacation`` AND ``tags_exclude=insurance``: keeps
    only transactions with vacation but not insurance.
    """
    tx_a = _tx(world, description="a", dt=date(2026, 5, 1))
    tx_b = _tx(world, description="b", dt=date(2026, 5, 2))
    db_session.add_all([tx_a, tx_b])
    await db_session.flush()
    insurance = Tag(
        org_id=world["org"].id, name="insurance", name_normalized="insurance"
    )
    vacation = Tag(
        org_id=world["org"].id, name="vacation", name_normalized="vacation"
    )
    db_session.add_all([insurance, vacation])
    await db_session.flush()
    db_session.add_all([
        # tx_a: insurance + vacation
        TransactionTag(transaction_id=tx_a.id, tag_id=insurance.id),
        TransactionTag(transaction_id=tx_a.id, tag_id=vacation.id),
        # tx_b: vacation only
        TransactionTag(transaction_id=tx_b.id, tag_id=vacation.id),
    ])
    await db_session.commit()

    txns, _ = await transaction_service.list_transactions(
        db_session, world["org"].id,
        tags=["vacation"], tags_exclude=["insurance"],
    )
    ids = sorted(tx.id for tx in txns)
    assert ids == [tx_b.id]
