"""Extended search semantics for ``transaction_service.list_transactions``.

The ``search`` filter must match against BOTH the transaction description
and the amount. Amount matching is sign-agnostic so a user searching for
``42`` finds the +42.00 and -42.00 rows alike. The predicates are OR'd
together so a numeric-looking string still hits descriptions that contain
those digits (e.g. "Order #1234").

These tests pin the end-to-end behaviour the user expects from the
transactions page search box.
"""
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
    other_acct = Account(
        org_id=org.id, name="Savings", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR",
    )
    db_session.add(other_acct)
    cat = Category(
        org_id=org.id, name="Groceries", slug="groceries",
        type=CategoryType.EXPENSE, is_system=False,
    )
    db_session.add(cat)
    await db_session.flush()
    return {"org": org, "account": acct, "other_account": other_acct, "category": cat}


def _make_tx(
    world,
    *,
    description: str,
    amount: str = "10.00",
    tx_type: TransactionType = TransactionType.EXPENSE,
    dt: date = date(2026, 5, 1),
    account_key: str = "account",
) -> Transaction:
    return Transaction(
        org_id=world["org"].id,
        account_id=world[account_key].id,
        category_id=world["category"].id,
        description=description,
        amount=Decimal(amount),
        type=tx_type,
        status=TransactionStatus.SETTLED,
        date=dt,
        settled_date=dt,
    )


# ── _parse_search_amount ──────────────────────────────────────────────────────

def test_parse_search_amount_plain_int():
    assert transaction_service._parse_search_amount("42") == Decimal("42")


def test_parse_search_amount_decimal():
    assert transaction_service._parse_search_amount("42.50") == Decimal("42.50")


def test_parse_search_amount_signed():
    # Leading sign is stripped — the caller matches both signs.
    assert transaction_service._parse_search_amount("-12.30") == Decimal("12.30")
    assert transaction_service._parse_search_amount("+7") == Decimal("7")


def test_parse_search_amount_currency_and_thousands():
    assert transaction_service._parse_search_amount("$1,234.56") == Decimal("1234.56")
    assert transaction_service._parse_search_amount("€ 99") == Decimal("99")


def test_parse_search_amount_non_numeric():
    assert transaction_service._parse_search_amount("groceries") is None
    assert transaction_service._parse_search_amount("") is None
    assert transaction_service._parse_search_amount("12abc") is None


# ── list_transactions(search=...) ────────────────────────────────────────────

async def test_search_pure_text_matches_description(db_session, world):
    db_session.add_all([
        _make_tx(world, description="Coffee shop", amount="3.50"),
        _make_tx(world, description="Grocery run", amount="22.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="coffee",
    )
    assert [r.description for r in rows] == ["Coffee shop"]


async def test_search_amount_matches_both_signs(db_session, world):
    """Searching '42' finds +42.00 AND -42.00 rows."""
    db_session.add_all([
        _make_tx(world, description="Refund", amount="42.00", tx_type=TransactionType.INCOME),
        _make_tx(world, description="Charge", amount="-42.00"),
        _make_tx(world, description="Unrelated", amount="10.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="42",
    )
    assert sorted(r.description for r in rows) == ["Charge", "Refund"]


async def test_search_decimal_amount_matches_equivalent_forms(db_session, world):
    """'42.50' matches stored 42.5 and -42.50."""
    db_session.add_all([
        _make_tx(world, description="A", amount="42.5"),
        _make_tx(world, description="B", amount="-42.50"),
        _make_tx(world, description="C", amount="42.49"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="42.50",
    )
    assert sorted(r.description for r in rows) == ["A", "B"]


async def test_search_amount_no_match_returns_empty(db_session, world):
    db_session.add_all([
        _make_tx(world, description="A", amount="10.00"),
        _make_tx(world, description="B", amount="20.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="9999.99",
    )
    assert rows == []


async def test_search_numeric_string_or_description_and_amount(db_session, world):
    """A numeric-looking input also hits descriptions that contain those digits.

    Predicates are OR'd — description ILIKE %1234% AND amount in [1234, -1234].
    """
    db_session.add_all([
        _make_tx(world, description="Order #1234", amount="9.99"),
        _make_tx(world, description="Wire", amount="1234.00"),
        _make_tx(world, description="Refund 1234", amount="-1234.00"),
        _make_tx(world, description="Unrelated", amount="5.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="1234",
    )
    assert sorted(r.description for r in rows) == [
        "Order #1234",
        "Refund 1234",
        "Wire",
    ]


async def test_search_combined_with_account_filter_is_and(db_session, world):
    """Other filters AND with search — search narrows within the account."""
    db_session.add_all([
        _make_tx(world, description="Coffee", amount="3.50", account_key="account"),
        _make_tx(world, description="Coffee", amount="3.50", account_key="other_account"),
        _make_tx(world, description="Other", amount="9.00", account_key="account"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session,
        world["org"].id,
        search="coffee",
        account_id=world["account"].id,
    )
    assert len(rows) == 1
    assert rows[0].account_id == world["account"].id


async def test_search_empty_string_skips_filter(db_session, world):
    """Empty / whitespace search matches everything (no predicate added)."""
    db_session.add_all([
        _make_tx(world, description="A", amount="1.00"),
        _make_tx(world, description="B", amount="2.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="   ",
    )
    assert sorted(r.description for r in rows) == ["A", "B"]


# ── TBD-463: search also matches the row's OWN category name ────────────────
#
# Case is varied in ASCII only: SQLite cannot reproduce MySQL's accent folding
# under utf8mb4_0900_ai_ci, so an accented test here would prove nothing.


async def _other_category(db_session, world, name: str, **kw) -> Category:
    cat = Category(
        org_id=kw.pop("org_id", world["org"].id), name=name,
        slug=name.lower(), type=CategoryType.EXPENSE, is_system=False, **kw,
    )
    db_session.add(cat)
    await db_session.flush()
    return cat


async def test_f5_search_matches_category_name(db_session, world):
    """F5: ``grocer`` finds a "Groceries" row whose description does not match."""
    dining = await _other_category(db_session, world, "Dining")
    db_session.add_all([
        _make_tx(world, description="Weekly shop"),
        Transaction(
            org_id=world["org"].id, account_id=world["account"].id,
            category_id=dining.id, description="Dinner", amount=Decimal("5"),
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
        ),
    ])
    await db_session.flush()

    rows, total = await transaction_service.list_transactions(
        db_session, world["org"].id, search="GROCER",
    )
    assert [r.description for r in rows] == ["Weekly shop"]
    assert total == 1


async def test_f6_search_name_does_not_expand_to_children(db_session, world):
    """F6: ``Groceries`` does NOT return a row on its child "Bulk"."""
    bulk = await _other_category(
        db_session, world, "Bulk", parent_id=world["category"].id,
    )
    db_session.add_all([
        _make_tx(world, description="Weekly shop"),
        Transaction(
            org_id=world["org"].id, account_id=world["account"].id,
            category_id=bulk.id, description="Pallet", amount=Decimal("5"),
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
        ),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="Groceries",
    )
    assert [r.description for r in rows] == ["Weekly shop"]


async def test_f8_search_name_is_org_scoped(db_session, world):
    """F8: another org's category name does not match.

    Transaction.org_id already scopes the rows, so the name lookup's own org
    scope is only observable through a row that references a foreign org's
    category id. No constraint forbids that row, so the test builds it.
    """
    other_org = Organization(name="Other", billing_cycle_day=1)
    db_session.add(other_org)
    await db_session.flush()
    foreign = await _other_category(db_session, world, "Pets", org_id=other_org.id)
    db_session.add(Transaction(
        org_id=world["org"].id, account_id=world["account"].id,
        category_id=foreign.id, description="Dinner", amount=Decimal("5"),
        type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        date=date(2026, 5, 1), settled_date=date(2026, 5, 1),
    ))
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="pets",
    )
    assert rows == []


async def test_guard_description_and_category_hit_returns_row_once(db_session, world):
    db_session.add(_make_tx(world, description="Groceries at the market"))
    await db_session.flush()

    rows, total = await transaction_service.list_transactions(
        db_session, world["org"].id, search="groceries",
    )
    assert len(rows) == 1
    assert total == 1


async def test_guard_amount_search_alongside_category_term(db_session, world):
    db_session.add_all([
        _make_tx(world, description="Charge", amount="42.00"),
        _make_tx(world, description="Other", amount="7.00"),
    ])
    await db_session.flush()

    rows, _ = await transaction_service.list_transactions(
        db_session, world["org"].id, search="42",
    )
    assert [r.description for r in rows] == ["Charge"]
