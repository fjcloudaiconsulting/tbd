"""``meta.truncated`` boundary fences for every reports source (TBD-484).

``truncated`` means "there was MORE than we returned". Three sources
computed it as ``len(out_rows) >= limit`` over rows the DATABASE had
already limited, so the flag could never be anything but a restatement
of "the result filled the limit" — a **false positive on complete
data**. TBD-430 makes the flag visible (a loud notice, and suppression
of the totals row / donut-hole total), so a complete 10-row result at
``limit=10`` hid a correct total and told the user it was incomplete.

⚠ The whole defect is an off-by-one, so every fence below drives BOTH
``exactly limit`` and ``limit + 1`` rows-available. A fence that only
drives an obviously-over-limit case (50 rows at ``limit=10``) passes
against the broken ``>=`` code too and proves nothing.

The two sources that were already correct (credit_utilization, networth
— both measure pre-slice, in Python) are fenced at the same boundary so
a later "let's make all five consistent" refactor onto ``>=`` is caught
rather than silently re-introducing the defect.
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
from app.models import Account, AccountType, Category, Organization
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation,
    Dataset,
    Dimension,
    Measure,
    MeasureField,
    QueryMeta,
    ReportsQuery,
    SortBy,
    SortDir,
    SortSpec,
)

# Small enough to seed exactly, large enough that the ORDER BY has work
# to do. The defect is scale-free: it fires at ANY limit.
LIMIT = 3


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


async def _org(db, *, type_slug="bank", type_name="Bank"):
    org = Organization(name="Org", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name=type_name, slug=type_slug, is_system=False)
    db.add(at)
    await db.flush()
    return org, at


# ─── transactions (reports_query_service.execute_query) ─────────────


async def _seed_transactions(db, n_groups: int) -> int:
    """One settled expense per category, ``n_groups`` distinct categories.

    Descending, distinct amounts so the default ``ORDER BY value DESC``
    is total — no tie can make the kept set ambiguous.
    """
    org, at = await _org(db)
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    db.add(acct)
    await db.flush()
    day = date(2026, 5, 15)
    for i in range(n_groups):
        cat = Category(org_id=org.id, name=f"Cat{i:02d}", type=CategoryType.EXPENSE)
        db.add(cat)
        await db.flush()
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="row", amount=Decimal(str(1000 - i)),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=day, settled_date=day,
            )
        )
    await db.flush()
    return org.id


def _transactions_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_transactions_exactly_limit_is_not_truncated(db_session):
    """A COMPLETE result that exactly fills the limit is NOT truncated.

    Kills ``len(out_rows) >= ast.limit``: the DB already applied
    ``LIMIT n``, so that predicate is true for every full page.
    """
    org_id = await _seed_transactions(db_session, LIMIT)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(db_session, org_id, _transactions_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_transactions_one_over_limit_is_truncated(db_session):
    """One more row than the limit → truncated, and the extra probe row
    never reaches the payload or ``row_count``."""
    org_id = await _seed_transactions(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(db_session, org_id, _transactions_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── accounts ───────────────────────────────────────────────────────


async def _seed_accounts(db, n_groups: int) -> int:
    org, at = await _org(db)
    for i in range(n_groups):
        db.add(
            Account(
                org_id=org.id, name=f"Acct{i:02d}", account_type_id=at.id,
                balance=Decimal(str(1000 - i)), currency="EUR", is_active=True,
            )
        )
    await db.flush()
    return org.id


def _accounts_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.ACCOUNTS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[Dimension.ACCOUNT],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_accounts_exactly_limit_is_not_truncated(db_session):
    org_id = await _seed_accounts(db_session, LIMIT)
    src = registry.get_source("accounts")
    rows, meta = await src.build_rows(db_session, org_id, _accounts_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_accounts_one_over_limit_is_truncated(db_session):
    org_id = await _seed_accounts(db_session, LIMIT + 1)
    src = registry.get_source("accounts")
    rows, meta = await src.build_rows(db_session, org_id, _accounts_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── recurring ──────────────────────────────────────────────────────


async def _seed_recurring(db, n_groups: int) -> int:
    org, at = await _org(db)
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    db.add(acct)
    await db.flush()
    for i in range(n_groups):
        cat = Category(org_id=org.id, name=f"Cat{i:02d}", type=CategoryType.EXPENSE)
        db.add(cat)
        await db.flush()
        db.add(
            RecurringTransaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description=f"tpl{i:02d}", amount=Decimal(str(1000 - i)),
                type="expense", frequency="monthly",
                next_due_date=date(2026, 1, 1), is_active=True,
            )
        )
    await db.flush()
    return org.id


def _recurring_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.RECURRING,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_recurring_exactly_limit_is_not_truncated(db_session):
    org_id = await _seed_recurring(db_session, LIMIT)
    src = registry.get_source("recurring")
    rows, meta = await src.build_rows(db_session, org_id, _recurring_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_recurring_one_over_limit_is_truncated(db_session):
    org_id = await _seed_recurring(db_session, LIMIT + 1)
    src = registry.get_source("recurring")
    rows, meta = await src.build_rows(db_session, org_id, _recurring_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── credit_utilization (already correct — regression fence) ────────


async def _seed_credit_cards(db, n_groups: int) -> int:
    org, at = await _org(db, type_slug="credit_card", type_name="Credit Card")
    for i in range(n_groups):
        db.add(
            Account(
                org_id=org.id, name=f"Card{i:02d}", account_type_id=at.id,
                balance=Decimal(str(-(1000 - i))), currency="EUR", is_active=True,
                credit_limit=Decimal("5000"),
            )
        )
    await db.flush()
    return org.id


def _credit_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.CREDIT_UTILIZATION,
        measure=Measure(agg=Aggregation.AVG, field=MeasureField.UTILIZATION_PCT),
        dimensions=[Dimension.ACCOUNT],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_credit_utilization_exactly_limit_is_not_truncated(db_session):
    """Regression fence. This source measures pre-slice (``> limit``) and
    is CORRECT today — the fence exists so a "make all five consistent"
    refactor onto ``>=`` is caught here rather than in production."""
    org_id = await _seed_credit_cards(db_session, LIMIT)
    src = registry.get_source("credit_utilization")
    rows, meta = await src.build_rows(db_session, org_id, _credit_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_credit_utilization_one_over_limit_is_truncated(db_session):
    org_id = await _seed_credit_cards(db_session, LIMIT + 1)
    src = registry.get_source("credit_utilization")
    rows, meta = await src.build_rows(db_session, org_id, _credit_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ─── networth (already correct — regression fence) ──────────────────


async def _seed_networth_months(db, n_months: int) -> int:
    """Opening balance in 2026-01 plus one settled txn per following
    month, so the series has exactly ``n_months`` buckets."""
    org, at = await _org(db)
    cat = Category(org_id=org.id, name="Misc", type=CategoryType.EXPENSE)
    db.add(cat)
    await db.flush()
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("1000"), currency="EUR", is_active=True,
        opening_balance=Decimal("1000"), opening_balance_date=date(2026, 1, 5),
    )
    db.add(acct)
    await db.flush()
    for m in range(2, n_months + 1):  # Jan is the opening-balance bucket
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="row", amount=Decimal("10"),
                type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
                date=date(2026, m, 10), settled_date=date(2026, m, 10),
            )
        )
    await db.flush()
    return org.id


def _networth_ast() -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.NETWORTH,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_WORTH),
        dimensions=[Dimension.MONTH],
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_networth_exactly_limit_is_not_truncated(db_session):
    """Regression fence — see the credit_utilization note above."""
    org_id = await _seed_networth_months(db_session, LIMIT)
    src = registry.get_source("networth")
    rows, meta = await src.build_rows(db_session, org_id, _networth_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is False


@pytest.mark.asyncio
async def test_networth_one_over_limit_is_truncated(db_session):
    org_id = await _seed_networth_months(db_session, LIMIT + 1)
    src = registry.get_source("networth")
    rows, meta = await src.build_rows(db_session, org_id, _networth_ast())

    assert len(rows) == LIMIT
    assert meta["row_count"] == LIMIT
    assert meta["truncated"] is True


# ════════════════════════════════════════════════════════════════════
# meta.truncated_end — WHICH end the limit dropped (TBD-484 follow-up)
# ════════════════════════════════════════════════════════════════════
#
# ⚠ Every fence below DRIVES the real query and reads what came back. None
# of them asserts a lookup table: reimplementing the client's inference map
# inside the fence would prove only that two copies of the guess agree.
#
# The decisive shape is the asc/desc SWAP — the SAME source over the SAME
# rows, differing only in ``sort.dir``, must report DIFFERENT ends. That is
# precisely what a ``(dataset, dimensions)``-keyed client map cannot see,
# and the reason this field exists.


def _sorted(by: SortBy, direction: SortDir) -> SortSpec:
    return SortSpec(by=by, dir=direction)


# ─── transactions: all four ends are reachable ──────────────────────


def _txn_ast(dimension: Dimension, sort: SortSpec | None) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[dimension],
        sort=sort,
        limit=LIMIT,
    )


async def _seed_transaction_months(db, n_months: int) -> int:
    """One settled expense per calendar month, DESCENDING amounts, so the
    chronological order and the value ranking are different orderings.

    If they coincided, a fence could not tell "ordered by month" apart from
    "ordered by value" and the asc/desc swap would prove nothing.
    """
    org, at = await _org(db)
    acct = Account(
        org_id=org.id, name="Chk", account_type_id=at.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    cat = Category(org_id=org.id, name="Misc", type=CategoryType.EXPENSE)
    db.add_all([acct, cat])
    await db.flush()
    for i in range(n_months):
        day = date(2026, 1 + i, 10)
        db.add(
            Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="row", amount=Decimal(str(1000 - i)),
                type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
                date=day, settled_date=day,
            )
        )
    await db.flush()
    return org.id


@pytest.mark.asyncio
async def test_transactions_value_desc_drops_the_lowest_ranked(db_session):
    org_id = await _seed_transactions(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    _rows, meta = await src.build_rows(
        db_session, org_id, _txn_ast(Dimension.CATEGORY, _sorted(SortBy.VALUE, SortDir.DESC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "lowest-ranked"


@pytest.mark.asyncio
async def test_transactions_value_asc_drops_the_highest_ranked(db_session):
    """⚠ THE DECISIVE SWAP. Same source, same rows, same limit as the test
    above — only ``sort.dir`` differs, and the answer inverts. A client map
    keyed on ``(dataset, dimensions)`` reports the same end for both."""
    org_id = await _seed_transactions(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    _rows, meta = await src.build_rows(
        db_session, org_id, _txn_ast(Dimension.CATEGORY, _sorted(SortBy.VALUE, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "highest-ranked"


@pytest.mark.asyncio
async def test_transactions_time_dimension_asc_drops_the_newest(db_session):
    """The ``emptyMultiSeries`` shape: line/area/stacked_bar seed
    ``sort: {by: "dimension", dir: "asc"}, limit: 100`` over ``month`` on
    ``transactions``. It keeps the OLDEST and drops the NEWEST — the case a
    source-keyed map words as "the first N rows"."""
    org_id = await _seed_transaction_months(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(
        db_session, org_id, _txn_ast(Dimension.MONTH, _sorted(SortBy.DIMENSION, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "newest"
    # Corroborate against the payload: the kept months really are the oldest.
    assert [r["month"] for r in rows] == ["2026-01", "2026-02", "2026-03"]


@pytest.mark.asyncio
async def test_transactions_time_dimension_desc_drops_the_oldest(db_session):
    """⚠ THE DECISIVE SWAP, chronological half."""
    org_id = await _seed_transaction_months(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    rows, meta = await src.build_rows(
        db_session, org_id, _txn_ast(Dimension.MONTH, _sorted(SortBy.DIMENSION, SortDir.DESC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "oldest"
    assert [r["month"] for r in rows] == ["2026-04", "2026-03", "2026-02"]


@pytest.mark.asyncio
async def test_transactions_complete_result_has_no_truncated_end(db_session):
    org_id = await _seed_transactions(db_session, LIMIT)
    src = registry.get_source("transactions")
    _rows, meta = await src.build_rows(db_session, org_id, _transactions_ast())
    assert meta["truncated"] is False
    assert meta["truncated_end"] is None


# ─── accounts ───────────────────────────────────────────────────────


def _accounts_ast_sorted(sort: SortSpec | None, dimension=Dimension.ACCOUNT) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.ACCOUNTS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[dimension],
        sort=sort,
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_accounts_value_desc_drops_the_lowest_ranked(db_session):
    org_id = await _seed_accounts(db_session, LIMIT + 1)
    src = registry.get_source("accounts")
    _rows, meta = await src.build_rows(
        db_session, org_id, _accounts_ast_sorted(_sorted(SortBy.VALUE, SortDir.DESC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "lowest-ranked"


@pytest.mark.asyncio
async def test_accounts_value_asc_drops_the_highest_ranked(db_session):
    """⚠ THE DECISIVE SWAP for this source."""
    org_id = await _seed_accounts(db_session, LIMIT + 1)
    src = registry.get_source("accounts")
    _rows, meta = await src.build_rows(
        db_session, org_id, _accounts_ast_sorted(_sorted(SortBy.VALUE, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "highest-ranked"


@pytest.mark.asyncio
async def test_accounts_non_time_dimension_sort_reports_no_end(db_session):
    """⚠ HONEST ABSENCE. Ordered by account NAME, the dropped rows are just
    the ones latest alphabetically — not a ranking end and not a
    chronological end. The source must say ``None`` and let the client fall
    back to the unqualified sentence, NOT guess "lowest-ranked"."""
    org_id = await _seed_accounts(db_session, LIMIT + 1)
    src = registry.get_source("accounts")
    _rows, meta = await src.build_rows(
        db_session, org_id, _accounts_ast_sorted(_sorted(SortBy.DIMENSION, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] is None


@pytest.mark.asyncio
async def test_accounts_complete_result_has_no_truncated_end(db_session):
    org_id = await _seed_accounts(db_session, LIMIT)
    src = registry.get_source("accounts")
    _rows, meta = await src.build_rows(db_session, org_id, _accounts_ast())
    assert meta["truncated"] is False
    assert meta["truncated_end"] is None


# ─── recurring ──────────────────────────────────────────────────────


def _recurring_ast_sorted(sort: SortSpec | None) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.RECURRING,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        sort=sort,
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_recurring_value_desc_drops_the_lowest_ranked(db_session):
    org_id = await _seed_recurring(db_session, LIMIT + 1)
    src = registry.get_source("recurring")
    _rows, meta = await src.build_rows(
        db_session, org_id, _recurring_ast_sorted(_sorted(SortBy.VALUE, SortDir.DESC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "lowest-ranked"


@pytest.mark.asyncio
async def test_recurring_value_asc_drops_the_highest_ranked(db_session):
    """⚠ THE DECISIVE SWAP for this source."""
    org_id = await _seed_recurring(db_session, LIMIT + 1)
    src = registry.get_source("recurring")
    _rows, meta = await src.build_rows(
        db_session, org_id, _recurring_ast_sorted(_sorted(SortBy.VALUE, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "highest-ranked"


@pytest.mark.asyncio
async def test_recurring_complete_result_has_no_truncated_end(db_session):
    org_id = await _seed_recurring(db_session, LIMIT)
    src = registry.get_source("recurring")
    _rows, meta = await src.build_rows(db_session, org_id, _recurring_ast())
    assert meta["truncated"] is False
    assert meta["truncated_end"] is None


# ─── credit_utilization (sorts in Python, same semantics) ───────────


def _credit_ast_sorted(sort: SortSpec | None) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.CREDIT_UTILIZATION,
        measure=Measure(agg=Aggregation.AVG, field=MeasureField.UTILIZATION_PCT),
        dimensions=[Dimension.ACCOUNT],
        sort=sort,
        limit=LIMIT,
    )


@pytest.mark.asyncio
async def test_credit_utilization_value_desc_drops_the_lowest_ranked(db_session):
    org_id = await _seed_credit_cards(db_session, LIMIT + 1)
    src = registry.get_source("credit_utilization")
    _rows, meta = await src.build_rows(
        db_session, org_id, _credit_ast_sorted(_sorted(SortBy.VALUE, SortDir.DESC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "lowest-ranked"


@pytest.mark.asyncio
async def test_credit_utilization_value_asc_drops_the_highest_ranked(db_session):
    """⚠ THE DECISIVE SWAP for this source — and it sorts in PYTHON, so it
    is a genuinely separate implementation of the same semantics."""
    org_id = await _seed_credit_cards(db_session, LIMIT + 1)
    src = registry.get_source("credit_utilization")
    _rows, meta = await src.build_rows(
        db_session, org_id, _credit_ast_sorted(_sorted(SortBy.VALUE, SortDir.ASC))
    )
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "highest-ranked"


@pytest.mark.asyncio
async def test_credit_utilization_complete_result_has_no_truncated_end(db_session):
    org_id = await _seed_credit_cards(db_session, LIMIT)
    src = registry.get_source("credit_utilization")
    _rows, meta = await src.build_rows(db_session, org_id, _credit_ast())
    assert meta["truncated"] is False
    assert meta["truncated_end"] is None


# ─── networth (ignores sort; reports from the branch it took) ───────


async def _seed_networth_currencies(db, n_currencies: int) -> int:
    """One account per currency, distinct balances. No time dimension is
    requested, so the source takes its value-desc branch."""
    org, at = await _org(db)
    for i in range(n_currencies):
        db.add(
            Account(
                org_id=org.id, name=f"Acct{i:02d}", account_type_id=at.id,
                balance=Decimal(str(1000 - i)), currency=f"C{i:02d}", is_active=True,
                opening_balance=Decimal(str(1000 - i)),
                opening_balance_date=date(2026, 1, 5),
            )
        )
    await db.flush()
    return org.id


@pytest.mark.asyncio
async def test_networth_time_series_drops_the_oldest(db_session):
    """⚠ The branch a source-keyed client map got wrong first. With a time
    dimension this source TAIL-keeps (``rows[-limit:]``) — it keeps the most
    recent periods and drops the OLDEST, the opposite end from every ranking
    source."""
    org_id = await _seed_networth_months(db_session, LIMIT + 1)
    src = registry.get_source("networth")
    rows, meta = await src.build_rows(db_session, org_id, _networth_ast())
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "oldest"
    assert [r["month"] for r in rows] == ["2026-02", "2026-03", "2026-04"]


@pytest.mark.asyncio
async def test_networth_without_a_time_dimension_drops_the_lowest_ranked(db_session):
    """⚠ THE DECISIVE SWAP for this source: the SAME dataset reports a
    different end depending on the branch, which is exactly why a
    ``dataset``-keyed map needed a compound key and still was not enough."""
    org_id = await _seed_networth_currencies(db_session, LIMIT + 1)
    src = registry.get_source("networth")
    q = ReportsQuery(
        dataset=Dataset.NETWORTH,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_WORTH),
        dimensions=[Dimension.CURRENCY],
        limit=LIMIT,
    )
    _rows, meta = await src.build_rows(db_session, org_id, q)
    assert meta["truncated"] is True
    assert meta["truncated_end"] == "lowest-ranked"


@pytest.mark.asyncio
async def test_networth_complete_result_has_no_truncated_end(db_session):
    org_id = await _seed_networth_months(db_session, LIMIT)
    src = registry.get_source("networth")
    _rows, meta = await src.build_rows(db_session, org_id, _networth_ast())
    assert meta["truncated"] is False
    assert meta["truncated_end"] is None


# ─── the wire ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_meta_model_carries_truncated_end_to_the_wire(db_session):
    """⚠ THE GUARD FOR THE GUARDS. Every fence above reads the source's raw
    meta dict. ``QueryMeta`` defaults to ``extra="ignore"``, so if the field
    were dropped from the model the router would silently discard it and
    all of them would stay green while the wire lost the value — the exact
    trap the ``warning`` field's own comment records.
    """
    org_id = await _seed_transaction_months(db_session, LIMIT + 1)
    src = registry.get_source("transactions")
    _rows, meta = await src.build_rows(
        db_session, org_id, _txn_ast(Dimension.MONTH, _sorted(SortBy.DIMENSION, SortDir.ASC))
    )
    serialized = QueryMeta(**meta).model_dump(mode="json")
    assert serialized["truncated"] is True
    assert serialized["truncated_end"] == "newest"

    complete_id = await _seed_transactions(db_session, LIMIT)
    _rows2, meta2 = await src.build_rows(db_session, complete_id, _transactions_ast())
    assert QueryMeta(**meta2).model_dump(mode="json")["truncated_end"] is None
