"""TBD-553 -- a signed ``net_amount`` measure for the transactions report source.

``Transaction.amount`` is an unsigned magnitude; direction lives in
``Transaction.type``. Every shipped "Net" widget summed the bare magnitude
column, so income PLUS expense rendered as "Net". This file fences the new
``net_amount`` measure: SUM compiles to a signed CASE, other aggs on it are
refused, the pre-existing ``amount`` measure/filter stay unsigned, the sign
is keyed on ``type`` (never on link-state), a real transfer pair nets to
zero per-currency while its two legs show opposite signs, and no other
report source can be asked for it.

Self-contained in-memory aiosqlite fixture, mirroring
``test_reports_transfer_axis.py`` / ``test_reports_currency_dimension.py``.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
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
from app.services import reports_query_service

P_START = datetime.date(2026, 6, 1)
P_END = datetime.date(2026, 6, 30)
D = datetime.date(2026, 6, 5)

SRC = registry.get_source("transactions")


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine_and_factory() -> AsyncIterator[tuple]:
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
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(engine_and_factory) -> AsyncIterator[AsyncSession]:
    _engine, factory = engine_and_factory
    async with factory() as session:
        yield session


async def _org(db, name: str = "Net Amount Org") -> dict:
    # Pin primary_currency explicitly. NOT because an unset value drops rows
    # -- it does the opposite: resolve_currency_scope returns
    # excluded_account_count == 0 when primary_currency is NULL, which
    # short-circuits org_currency_filter to true() (currency_service.py). The
    # reason to pin it is the MULTI-CURRENCY cases below, where the scope
    # clause must be armed to mean anything; leaving it NULL there would
    # disarm the very clause under test and the fence would pass for the
    # wrong reason. In the single-EUR-account cases it is inert either way.
    org = Organization(name=name, billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug=None)
    db.add(at)
    await db.flush()
    cat = Category(org_id=org.id, name="General", slug="general", type=CategoryType.EXPENSE)
    db.add(cat)
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    return {"org": org, "at": at, "cat": cat}


async def _acct(db, o, name: str, currency: str = "EUR") -> Account:
    # Pin accounts.currency explicitly (same reason as the org above).
    a = Account(org_id=o["org"].id, account_type_id=o["at"].id, name=name,
                balance=Decimal("0"), currency=currency)
    db.add(a)
    await db.flush()
    return a


async def _txn(db, o, acct, amount: Decimal, ttype: TransactionType, *,
               linked_id: int | None = None) -> Transaction:
    t = Transaction(
        org_id=o["org"].id, account_id=acct.id, category_id=o["cat"].id,
        description="row", amount=amount, type=ttype,
        status=TransactionStatus.SETTLED, date=D, settled_date=D,
        linked_transaction_id=linked_id,
    )
    db.add(t)
    await db.flush()
    return t


def _q(agg: Aggregation, field: MeasureField, *, dimensions=None, filters=None,
       include_non_reportable: bool = False) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=agg, field=field),
        dimensions=dimensions or [],
        filters=filters or [],
        limit=100,
        include_non_reportable=include_non_reportable,
    )


# ── fence: net_sum_is_signed ────────────────────────────────────────────


async def test_net_sum_is_signed(db):
    """2000 income + 3000 expense -> -1000.

    Inverted deliberately: naive gross is 5000, abs() of the correct answer
    is 1000, and the correct signed answer is -1000. All three are
    distinguishable, so neither a gross-sum mutant nor an abs()-of-net
    mutant survives.
    """
    o = await _org(db)
    a = await _acct(db, o, "Checking")
    await _txn(db, o, a, Decimal("2000"), TransactionType.INCOME)
    await _txn(db, o, a, Decimal("3000"), TransactionType.EXPENSE)
    await db.commit()

    rows, _meta = await reports_query_service.execute_query(
        db, _q(Aggregation.SUM, MeasureField.NET_AMOUNT), org_id=o["org"].id,
    )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["value"])) == Decimal("-1000")


# ── fence: sum_amount_is_still_unsigned ─────────────────────────────────


async def test_sum_amount_is_still_unsigned(db):
    """Same rows, ``sum(amount)`` -> 5000 (gross, unchanged).

    Plus: ``FilterField.AMOUNT`` stays a magnitude -- ``amount >= 100``
    still selects a -250 (stored as 250, typed EXPENSE) row.
    """
    o = await _org(db)
    a = await _acct(db, o, "Checking")
    await _txn(db, o, a, Decimal("2000"), TransactionType.INCOME)
    await _txn(db, o, a, Decimal("3000"), TransactionType.EXPENSE)
    await db.commit()

    rows, _meta = await reports_query_service.execute_query(
        db, _q(Aggregation.SUM, MeasureField.AMOUNT), org_id=o["org"].id,
    )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["value"])) == Decimal("5000")

    # amount >= 100 filter on a -250 (magnitude 250) expense: still selected.
    o2 = await _org(db, "Amount Filter Org")
    a2 = await _acct(db, o2, "Checking")
    expense = await _txn(db, o2, a2, Decimal("250"), TransactionType.EXPENSE)
    await db.commit()

    rows2, _meta2 = await reports_query_service.execute_query(
        db,
        _q(
            Aggregation.COUNT, MeasureField.ID,
            filters=[Filter(field=FilterField.AMOUNT, op=FilterOp.GTE, value=100)],
        ),
        org_id=o2["org"].id,
    )
    assert rows2[0]["value"] == 1, (
        "amount >= 100 must still select a -250 expense: the filter is on "
        "the magnitude, not the signed value"
    )
    del expense  # only used to construct the row


# ── fence: net_sign_is_keyed_on_type ────────────────────────────────────


async def test_net_sign_is_keyed_on_type(db):
    """A row whose type and link-state DISAGREE.

    ``type=INCOME`` but carries a non-null, RECIPROCAL ``linked_transaction_id``
    (a real transfer leg). ``include_non_reportable=True`` so
    ``non_reverted_transaction_filter`` (``balance_contribution_filter``,
    which keeps reciprocal transfer legs) admits it, and the default
    reportable filter -- which drops every linked row outright -- is not
    what is exercised here. The correct sign is +100 (keyed on ``type``); a
    sign keyed on link-state (linked => negative, regardless of type) would
    answer -100.
    """
    o = await _org(db)
    a = await _acct(db, o, "Checking")
    other = await _txn(db, o, a, Decimal("1"), TransactionType.EXPENSE)
    leg = await _txn(db, o, a, Decimal("100"), TransactionType.INCOME, linked_id=other.id)
    other.linked_transaction_id = leg.id
    await db.flush()

    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            Aggregation.SUM, MeasureField.NET_AMOUNT,
            filters=[Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ, value=a.id)],
            include_non_reportable=True,
        ),
        org_id=o["org"].id,
    )
    assert Decimal(str(rows[0]["value"])) == Decimal("99")  # +100 (income leg) - 1 (expense)


# ── fence: net_agg_refused ──────────────────────────────────────────────


async def test_net_agg_refused():
    """Drives ``source.validate()``, never ``execute_query`` (which never
    calls ``validate()`` and would pass with the guard absent)."""
    for agg in (Aggregation.AVG, Aggregation.COUNT, Aggregation.DISTINCT):
        query = ReportsQuery(
            dataset=Dataset.TRANSACTIONS,
            measure=Measure(agg=agg, field=MeasureField.NET_AMOUNT),
            limit=100,
        )
        with pytest.raises(ValueError, match="net_amount"):
            SRC.validate(query)

    # sum(net_amount) still passes.
    SRC.validate(ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_AMOUNT),
        limit=100,
    ))


# ── fence: transfer_pair_nets_to_zero ───────────────────────────────────


async def test_transfer_pair_nets_to_zero_but_legs_are_opposite_signs(db):
    """0.00 is empty-satisfiable, so the grouped-by-account half is the
    load-bearing assertion and must be in THIS test: exactly 2 rows,
    {+A, -A}. A non-transfer baseline row proves the ungrouped total isn't
    coincidentally 0 for the wrong reason (an unsatisfiable selection
    returning zero rows also sums to 0.00)."""
    o = await _org(db)
    checking = await _acct(db, o, "Checking")
    savings = await _acct(db, o, "Savings")

    out_leg = await _txn(db, o, checking, Decimal("400"), TransactionType.EXPENSE)
    in_leg = await _txn(db, o, savings, Decimal("400"), TransactionType.INCOME)
    out_leg.linked_transaction_id = in_leg.id
    in_leg.linked_transaction_id = out_leg.id
    await db.flush()

    baseline = await _txn(db, o, checking, Decimal("25"), TransactionType.EXPENSE)
    del baseline
    await db.commit()

    transfer_filter = [Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=True)]

    # Ungrouped: the pair alone cancels to 0.00 (isolated by transfer=true,
    # which excludes the non-transfer baseline).
    rows_flat, _meta = await reports_query_service.execute_query(
        db,
        _q(Aggregation.SUM, MeasureField.NET_AMOUNT, filters=transfer_filter,
           include_non_reportable=True),
        org_id=o["org"].id,
    )
    assert Decimal(str(rows_flat[0]["value"])) == Decimal("0")

    # Everything including the baseline: non-zero, so an unsatisfiable
    # selection (which would ALSO return 0.00) is distinguishable.
    rows_all, _meta2 = await reports_query_service.execute_query(
        db,
        _q(Aggregation.SUM, MeasureField.NET_AMOUNT, include_non_reportable=True),
        org_id=o["org"].id,
    )
    assert Decimal(str(rows_all[0]["value"])) == Decimal("-25")

    # Grouped by account: exactly 2 rows, opposite signs.
    rows_grouped, _meta3 = await reports_query_service.execute_query(
        db,
        _q(Aggregation.SUM, MeasureField.NET_AMOUNT, dimensions=[Dimension.ACCOUNT],
           filters=transfer_filter, include_non_reportable=True),
        org_id=o["org"].id,
    )
    assert len(rows_grouped) == 2
    by_account = {r["account"]: Decimal(str(r["value"])) for r in rows_grouped}
    assert set(by_account.values()) == {Decimal("400"), Decimal("-400")}
    assert by_account["Checking"] == Decimal("-400")
    assert by_account["Savings"] == Decimal("400")


# ── fence: net_amount_refused_by_other_sources ──────────────────────────


def test_net_amount_refused_by_other_sources():
    for key in ("accounts", "recurring", "networth", "credit_utilization"):
        src = registry.get_source(key)
        query = ReportsQuery(
            dataset=Dataset(key),
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_AMOUNT),
            limit=100,
        )
        # match= matters: a bare ValueError would also be satisfied by an
        # unrelated failure (a bad Dataset, a missing dimension), so the
        # fence would pass without the catalog ever refusing the field.
        with pytest.raises(ValueError, match="net_amount"):
            src.validate(query)


# ── fence: transfer_legs_on_one_account_net (DoD item 2) ────────────────


async def test_transfer_legs_on_one_account_net(db):
    """A savings account carrying TWO DISTINCT transfer pairs (not one pair
    with two legs): a +500 inbound leg (partner on Checking) and a -200
    outbound leg (partner on a second account). ``transfer=true`` + an
    account filter + ``sum(net_amount)`` -> 300.

    The three candidate answers are mutually distinct, so no mutant survives
    by coincidence:
      - unsigned reading:            500 + 200 = 700
      - one-direction-only reading:  500 (drops the outbound leg entirely)
      - correct signed reading:      500 - 200 = 300

    ``org.primary_currency`` and every account's currency are pinned
    explicitly (via ``_org``/``_acct``), or ``org_currency_filter`` may drop
    legs and this would pass for the wrong reason.
    """
    o = await _org(db)
    savings = await _acct(db, o, "Savings")
    checking = await _acct(db, o, "Checking")
    other = await _acct(db, o, "Other")

    # Pair 1: +500 into Savings, partner leg (-500, EXPENSE) on Checking.
    out1 = await _txn(db, o, checking, Decimal("500"), TransactionType.EXPENSE)
    in1 = await _txn(db, o, savings, Decimal("500"), TransactionType.INCOME)
    out1.linked_transaction_id = in1.id
    in1.linked_transaction_id = out1.id

    # Pair 2: -200 out of Savings, partner leg (+200, INCOME) on Other.
    out2 = await _txn(db, o, savings, Decimal("200"), TransactionType.EXPENSE)
    in2 = await _txn(db, o, other, Decimal("200"), TransactionType.INCOME)
    out2.linked_transaction_id = in2.id
    in2.linked_transaction_id = out2.id

    await db.flush()
    await db.commit()

    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            Aggregation.SUM, MeasureField.NET_AMOUNT,
            filters=[
                Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=True),
                Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ, value=savings.id),
            ],
            include_non_reportable=True,
        ),
        org_id=o["org"].id,
    )
    assert len(rows) == 1
    assert Decimal(str(rows[0]["value"])) == Decimal("300")
