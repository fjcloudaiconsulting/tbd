"""Service-layer tests for description-suggestion autocomplete (L3.2 Wave 2A).

Pins the ranking contract from
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
§5:

1. Prefix match first.
2. Then frequency (use_count DESC).
3. Then recency (last_used DESC).

And the privacy/org-scope rules:
- No cross-org leak.
- Type filter is respected.
- ``q`` shorter than 2 chars: handled at the router; the service is
  permissive so it can serve the "recent items" path when ``q`` is None.
- ``limit`` cap.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.account import Account, AccountType
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization
from app.services.transaction_suggestions_service import (
    get_description_suggestions,
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


async def _seed_org(factory, name: str) -> tuple[int, int, int]:
    """Seed an org with one account-type, one account, and one category.

    Returns ``(org_id, account_id, category_id)``.
    """
    async with factory() as db:
        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.flush()
        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.flush()
        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Main",
            balance=Decimal("0.00"),
            currency="EUR",
        )
        cat = Category(
            org_id=org.id,
            name="Groceries",
            type=CategoryType.EXPENSE,
        )
        db.add_all([acct, cat])
        await db.commit()
        return org.id, acct.id, cat.id


async def _add_category(factory, org_id: int, name: str, type_: CategoryType) -> int:
    async with factory() as db:
        c = Category(org_id=org_id, name=name, type=type_)
        db.add(c)
        await db.commit()
        return c.id


async def _add_tx(
    factory,
    *,
    org_id: int,
    account_id: int,
    category_id: int,
    description: str,
    date: datetime.date,
    amount: str = "10.00",
    type_: TransactionType = TransactionType.EXPENSE,
) -> None:
    async with factory() as db:
        tx = Transaction(
            org_id=org_id,
            account_id=account_id,
            category_id=category_id,
            description=description,
            amount=Decimal(amount),
            type=type_,
            status=TransactionStatus.SETTLED,
            date=date,
            settled_date=date,
            is_imported=False,
        )
        db.add(tx)
        await db.commit()


# ── ranking ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prefix_matches_rank_above_substring(session_factory):
    """Prefix matches MUST come back before substring matches even if
    the substring match is more frequent."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "RankOrg")
    # Substring match used 5 times.
    for i in range(5):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Super Albert Market",
            date=datetime.date(2026, 5, 1),
        )
    # Prefix match used only 2 times.
    for i in range(2):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Heijn",
            date=datetime.date(2026, 4, 15),
        )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="Alb", limit=10
        )

    assert len(out) == 2
    assert out[0].description == "Albert Heijn"
    assert out[1].description == "Super Albert Market"


@pytest.mark.asyncio
async def test_within_prefix_tier_frequency_wins(session_factory):
    """Among prefix matches, higher use_count wins."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "FreqOrg")
    for i in range(3):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Cuyp",
            date=datetime.date(2026, 5, 1),
        )
    for i in range(7):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Heijn",
            date=datetime.date(2026, 4, 1),
        )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="Alb", limit=10
        )

    assert [s.description for s in out] == ["Albert Heijn", "Albert Cuyp"]
    assert out[0].use_count == 7
    assert out[1].use_count == 3


@pytest.mark.asyncio
async def test_within_same_frequency_recency_wins(session_factory):
    """Two prefix-matched descriptions with equal use_count: most-recent
    last_used wins the tiebreaker."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "RecencyOrg")
    for d in [datetime.date(2026, 5, 11), datetime.date(2026, 5, 12)]:
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Heijn",
            date=d,
        )
    for d in [datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)]:
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Cuyp",
            date=d,
        )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="Alb", limit=10
        )

    assert [s.description for s in out] == ["Albert Heijn", "Albert Cuyp"]
    assert out[0].last_used == datetime.date(2026, 5, 12)


@pytest.mark.asyncio
async def test_empty_q_returns_top_n_most_used(session_factory):
    """When q is None (router-permissive path), service returns the
    top-N most-used descriptions for the org+type — the "recent items"
    hint used by an empty input."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "EmptyOrg")
    for _ in range(4):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Coffee",
            date=datetime.date(2026, 5, 10),
        )
    for _ in range(2):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Lunch",
            date=datetime.date(2026, 5, 11),
        )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q=None, limit=10
        )

    assert [s.description for s in out] == ["Coffee", "Lunch"]


@pytest.mark.asyncio
async def test_limit_caps_results(session_factory):
    """Service respects the `limit` arg."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "LimitOrg")
    for i in range(7):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description=f"Merchant-{i:02d}",
            date=datetime.date(2026, 5, 1),
        )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q=None, limit=3
        )
    assert len(out) == 3


# ── isolation ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_other_org_data_does_not_leak(session_factory):
    """Suggestions filter strictly on org_id."""
    org_a, acct_a, cat_a = await _seed_org(session_factory, "OrgA")
    org_b, acct_b, cat_b = await _seed_org(session_factory, "OrgB")
    await _add_tx(
        session_factory,
        org_id=org_b,
        account_id=acct_b,
        category_id=cat_b,
        description="Albert Heijn",
        date=datetime.date(2026, 5, 1),
    )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_a, type="expense", q="Alb", limit=10
        )
    assert out == []


@pytest.mark.asyncio
async def test_type_filter_excludes_other_types(session_factory):
    """Only the requested transaction type contributes to the ranking."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "TypeOrg")
    income_cat = await _add_category(
        session_factory, org_id, "Salary", CategoryType.INCOME
    )
    for _ in range(5):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Heijn",
            date=datetime.date(2026, 5, 1),
            type_=TransactionType.EXPENSE,
        )
    for _ in range(5):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=income_cat,
            description="Albert Heijn",
            date=datetime.date(2026, 5, 1),
            type_=TransactionType.INCOME,
        )

    async with session_factory() as db:
        out_exp = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="Alb", limit=10
        )
        out_inc = await get_description_suggestions(
            db, org_id=org_id, type="income", q="Alb", limit=10
        )

    assert len(out_exp) == 1 and out_exp[0].use_count == 5
    assert out_exp[0].category_id == cat_id
    assert len(out_inc) == 1 and out_inc[0].use_count == 5
    assert out_inc[0].category_id == income_cat


# ── category pairing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_category_is_most_used_pair(session_factory):
    """When a description appears with multiple categories, the
    suggestion reports the most-frequently-paired one."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "CatPairOrg")
    other_cat = await _add_category(
        session_factory, org_id, "Coffee Shop", CategoryType.EXPENSE
    )
    # Albert Heijn paired with Groceries (cat_id) 4 times.
    for _ in range(4):
        await _add_tx(
            session_factory,
            org_id=org_id,
            account_id=account_id,
            category_id=cat_id,
            description="Albert Heijn",
            date=datetime.date(2026, 4, 1),
        )
    # And with Coffee Shop just once.
    await _add_tx(
        session_factory,
        org_id=org_id,
        account_id=account_id,
        category_id=other_cat,
        description="Albert Heijn",
        date=datetime.date(2026, 5, 11),
    )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="Alb", limit=10
        )
    assert len(out) == 1
    s = out[0]
    assert s.description == "Albert Heijn"
    assert s.use_count == 5
    assert s.category_id == cat_id
    assert s.category_name == "Groceries"


# ── LIKE-metacharacter safety ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_like_metacharacters_in_q_are_escaped(session_factory):
    """A query containing '%' MUST be treated as a literal, not a
    wildcard. Otherwise a one-char q like '%' could match everything,
    bypassing the 2-char minimum's privacy intent."""
    org_id, account_id, cat_id = await _seed_org(session_factory, "EscOrg")
    await _add_tx(
        session_factory,
        org_id=org_id,
        account_id=account_id,
        category_id=cat_id,
        description="Albert Heijn",
        date=datetime.date(2026, 5, 1),
    )
    await _add_tx(
        session_factory,
        org_id=org_id,
        account_id=account_id,
        category_id=cat_id,
        description="50% Off Sale",
        date=datetime.date(2026, 5, 2),
    )

    async with session_factory() as db:
        out = await get_description_suggestions(
            db, org_id=org_id, type="expense", q="50%", limit=10
        )

    assert [s.description for s in out] == ["50% Off Sale"]
