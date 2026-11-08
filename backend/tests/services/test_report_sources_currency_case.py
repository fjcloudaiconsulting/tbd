"""TBD-550 — the snapshot report sources fold legacy currency case and padding.

``accounts.currency`` was free text before TBD-325 PR 1, and ``AccountUpdate``
has no currency field, so a stored ``'eur'`` or ``'EUR '`` cannot be repaired
through the product. TBD-507 made the transactions source compare
``UPPER(TRIM(accounts.currency))`` on both the GROUP BY and the filter side; the
accounts, recurring, net-worth and credit-utilization sources compared the bare
column. They now share ``transaction_filters.account_currency_key()``.

One fixture serves every source: an org holding ``'EUR'``, ``'eur'``,
``'EUR '`` and ``'USD'`` accounts. Each account is a credit card with a negative
balance, an opening balance and a recurring template, so the same four rows
feed all four sources. The USD account is what keeps the filter fences honest:
a filter that is ignored altogether returns the USD money too, and goes red.

⚠ RED ON SQLITE, VACUOUS ON MYSQL for the CASE half -- and SQLite is where CI
runs (every shard but the migration checks). MySQL's case-insensitive collation
folds ``'eur'`` into ``'EUR'`` in a comparison and in a GROUP BY, so the
bare-column mutant passes there. Do not cite these tests as MySQL coverage of
the case fold. The ``'EUR '`` row fences the ``TRIM`` half, which IS a real
MySQL defect (the collation is NO PAD), so that half is not SQLite-only.
Same disclaimer as ``test_reports_currency_dimension.py::test_f4/test_f5``.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.reports import sources as registry
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
from app.schemas.reports_query import (
    Filter,
    FilterField,
    FilterOp,
    Measure,
    ReportsQuery,
)

OPENED = datetime.date(2026, 1, 5)
SETTLED = datetime.date(2026, 2, 10)

# currency as stored -> amount. Balances are stored NEGATIVE (credit cards), so
# credit utilization sees an outstanding amount and net worth a liability.
UPPER, LOWER, PADDED, USD = Decimal(100), Decimal(30), Decimal(7), Decimal(5000)
EUR_TOTAL = UPPER + LOWER + PADDED  # 137
# One settled income on the LOWERCASE account, so net worth's second stream
# (settled deltas) carries a legacy row too, not only its opening-balance stream.
LOWER_INCOME = Decimal(20)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def org_id(db) -> int:
    org = Organization(name="Legacy Case Org", billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Credit Card", slug="credit_card", is_system=True)
    cat = Category(org_id=org.id, name="Bills", slug="bills", type=CategoryType.EXPENSE)
    db.add_all([at, cat])
    await db.flush()
    for stored, amount in (("EUR", UPPER), ("eur", LOWER), ("EUR ", PADDED), ("USD", USD)):
        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name=f"Card ({stored!r})",
            balance=-amount,
            opening_balance=-amount,
            opening_balance_date=OPENED,
            credit_limit=Decimal(10000),
            currency=stored,
            is_active=True,
        )
        db.add(acct)
        await db.flush()
        db.add(RecurringTransaction(
            org_id=org.id, account_id=acct.id, category_id=cat.id,
            description="Subscription", amount=amount, type="expense",
            frequency="monthly", next_due_date=OPENED, is_active=True,
        ))
        if stored == "eur":
            db.add(Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat.id,
                description="Refund", amount=LOWER_INCOME, type=TransactionType.INCOME,
                status=TransactionStatus.SETTLED, date=SETTLED, settled_date=SETTLED,
            ))
    await db.commit()
    return org.id


_MEASURE = {
    Dataset.ACCOUNTS: Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
    Dataset.RECURRING: Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
    Dataset.NETWORTH: Measure(agg=Aggregation.SUM, field=MeasureField.NET_WORTH),
    Dataset.CREDIT_UTILIZATION: Measure(agg=Aggregation.SUM, field=MeasureField.OUTSTANDING),
}

# What each source reports for EUR and USD over the fixture.
_EXPECTED = {
    Dataset.ACCOUNTS: {"EUR": -EUR_TOTAL, "USD": -USD},
    Dataset.RECURRING: {"EUR": EUR_TOTAL, "USD": USD},
    Dataset.NETWORTH: {"EUR": -EUR_TOTAL + LOWER_INCOME, "USD": -USD},
    Dataset.CREDIT_UTILIZATION: {"EUR": EUR_TOTAL, "USD": USD},
}


async def _run(db, org_id: int, dataset: Dataset, *, dimensions=(), filters=()):
    q = ReportsQuery(
        dataset=dataset,
        measure=_MEASURE[dataset],
        dimensions=list(dimensions),
        filters=list(filters),
        limit=100,
    )
    src = registry.get_source(dataset.value)
    src.validate(q)
    rows, _meta = await src.build_rows(db, org_id, q)
    return rows


async def _assert_dimension_folds(db, org_id, dataset):
    rows = await _run(db, org_id, dataset, dimensions=[Dimension.CURRENCY])
    got = {r["currency"]: Decimal(str(r["value"])) for r in rows}
    assert got == _EXPECTED[dataset], (
        f"{dataset.value}: expected one canonical EUR partition; got {got}"
    )


async def _assert_filter_matches(db, org_id, dataset):
    rows = await _run(
        db, org_id, dataset,
        filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"])],
    )
    total = sum(Decimal(str(r["value"])) for r in rows)
    assert total == _EXPECTED[dataset]["EUR"], (
        f"{dataset.value}: a legacy 'eur' / 'EUR ' account dropped out of the "
        f"EUR filter (or USD leaked in); got {rows}"
    )


# ── accounts ────────────────────────────────────────────────────────────────


async def test_accounts_currency_dimension_folds_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` on the accounts GROUP BY.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'`` (the collation folds case);
    the ``'EUR '`` row is real MySQL coverage of the TRIM half. See module doc.
    """
    await _assert_dimension_folds(db, org_id, Dataset.ACCOUNTS)


async def test_accounts_currency_filter_matches_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` in the accounts currency filter.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_filter_matches(db, org_id, Dataset.ACCOUNTS)


# ── recurring ───────────────────────────────────────────────────────────────


async def test_recurring_currency_dimension_folds_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` on the recurring GROUP BY.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_dimension_folds(db, org_id, Dataset.RECURRING)


async def test_recurring_currency_filter_matches_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` in the recurring currency filter.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_filter_matches(db, org_id, Dataset.RECURRING)


# ── net worth ───────────────────────────────────────────────────────────────


async def test_networth_currency_dimension_folds_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` in either net-worth stream's SELECT/GROUP BY.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc. The two
    streams are merged in PYTHON on the SQL-emitted currency string, which does
    not fold case: the SQL key must already be canonical. On MySQL the bare
    column's GROUP BY label is whichever row the engine picked per stream, so
    against the mutant this is flaky there rather than vacuous.
    """
    await _assert_dimension_folds(db, org_id, Dataset.NETWORTH)


async def test_networth_currency_filter_matches_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` in either net-worth stream's filter.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_filter_matches(db, org_id, Dataset.NETWORTH)


async def test_networth_time_series_is_one_eur_series(db, org_id):
    """Net worth over months: one EUR series, opening and settled streams merged.

    The ``'eur'`` account's opening balance (January) and its settled income
    (February) arrive in DIFFERENT streams, so this also fences the in-Python
    merge. ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    rows = await _run(db, org_id, Dataset.NETWORTH, dimensions=[Dimension.MONTH, Dimension.CURRENCY])
    got = {(r["month"], r["currency"]): Decimal(str(r["value"])) for r in rows}
    assert got == {
        ("2026-01", "EUR"): -EUR_TOTAL,
        ("2026-01", "USD"): -USD,
        ("2026-02", "EUR"): -EUR_TOTAL + LOWER_INCOME,
    }, got


# ── credit utilization ──────────────────────────────────────────────────────


async def test_credit_utilization_currency_dimension_folds_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` on the credit-utilization GROUP BY.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_dimension_folds(db, org_id, Dataset.CREDIT_UTILIZATION)


async def test_credit_utilization_currency_filter_matches_legacy_case(db, org_id):
    """Kills bare ``Account.currency`` in the credit-utilization currency filter.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    await _assert_filter_matches(db, org_id, Dataset.CREDIT_UTILIZATION)


async def test_credit_utilization_implicit_currency_key_folds_legacy_case(db, org_id):
    """No currency dimension requested: the source still groups by currency.

    That implicit group key is a separate site from the dimension mapping. With
    the bare column, the three EUR cards split into three rows.
    ⚠ RED ON SQLITE, VACUOUS ON MYSQL for ``'eur'``; see module doc.
    """
    rows = await _run(db, org_id, Dataset.CREDIT_UTILIZATION)
    got = {r["currency"]: Decimal(str(r["value"])) for r in rows}
    assert got == _EXPECTED[Dataset.CREDIT_UTILIZATION], got
