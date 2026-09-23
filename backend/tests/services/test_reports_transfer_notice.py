"""TBD-471 RULING 3 — ``_transfer_notice`` and its composition with
``_currency_notice`` into the single ``meta.warning`` slot.

Fixture helpers mirror ``test_reports_transfer_axis.py`` (org/account/txn/pair
builders) rather than importing them, so this file's fixtures stay legible on
their own and a change to the transfer-axis fixture cannot silently alter
what this file measures.
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
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
from app.schemas.reports_query import (
    Filter,
    FilterField,
    FilterOp,
    Measure,
    ReportsQuery,
)
from app.services import reports_query_service
from app.services.reports_query_service import _TRANSFER_NOTICE, _transfer_notice

P_START = datetime.date(2026, 6, 1)
P_END = datetime.date(2026, 6, 30)
D = datetime.date(2026, 6, 5)

IN_AMT = Decimal("500")
OUT_AMT = Decimal("200")


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


async def _org(db, *, second_currency: bool = False) -> dict:
    org = Organization(name="Notice Org", billing_cycle_day=1, primary_currency="EUR")
    db.add(org)
    await db.flush()
    checking_t = AccountType(org_id=org.id, name="Checking", slug=None)
    savings_t = AccountType(org_id=org.id, name="Savings", slug=None)
    db.add_all([checking_t, savings_t])
    await db.flush()
    cat = Category(org_id=org.id, name="Transfer", slug="transfer", type=CategoryType.EXPENSE)
    db.add(cat)
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    checking = Account(org_id=org.id, account_type_id=checking_t.id, name="Checking",
                        balance=Decimal("0"), currency="EUR")
    savings = Account(org_id=org.id, account_type_id=savings_t.id, name="Savings",
                       balance=Decimal("0"), currency="EUR")
    db.add_all([checking, savings])
    await db.flush()
    out = {"org": org, "cat": cat, "checking": checking, "savings": savings}
    if second_currency:
        usd = Account(org_id=org.id, account_type_id=checking_t.id, name="USD Checking",
                      balance=Decimal("0"), currency="USD")
        db.add(usd)
        await db.flush()
        out["usd_account"] = usd
    await db.commit()
    return out


async def _pair(db, o, src, dst, amount: Decimal) -> None:
    outgoing = Transaction(
        org_id=o["org"].id, account_id=src.id, category_id=o["cat"].id,
        description="out", amount=amount, type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED, date=D, settled_date=D,
    )
    incoming = Transaction(
        org_id=o["org"].id, account_id=dst.id, category_id=o["cat"].id,
        description="in", amount=amount, type=TransactionType.INCOME,
        status=TransactionStatus.SETTLED, date=D, settled_date=D,
    )
    db.add_all([outgoing, incoming])
    await db.flush()
    outgoing.linked_transaction_id = incoming.id
    incoming.linked_transaction_id = outgoing.id
    await db.commit()


@pytest_asyncio.fixture
async def world(db) -> dict:
    o = await _org(db)
    await _pair(db, o, o["checking"], o["savings"], IN_AMT)
    await _pair(db, o, o["savings"], o["checking"], OUT_AMT)
    return o


@pytest_asyncio.fixture
async def multi_currency_world(db) -> dict:
    """Non-NULL ``primary_currency`` PLUS a second-currency account, so
    ``_currency_notice`` fires ``_SCOPED`` -> non-None alongside the transfer
    sentence."""
    o = await _org(db, second_currency=True)
    await _pair(db, o, o["checking"], o["savings"], IN_AMT)
    await _pair(db, o, o["savings"], o["checking"], OUT_AMT)
    return o


def _q(dimensions=None, filters=None, include_non_reportable: bool = False) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=dimensions or [],
        filters=filters or [],
        limit=100,
        include_non_reportable=include_non_reportable,
    )


def _transfer_only(*, value: bool = True) -> Filter:
    return Filter(field=FilterField.TRANSFER, op=FilterOp.EQ, value=value)


def _account_id(value: int) -> Filter:
    return Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.EQ, value=value)


# ── fence: transfer_notice_fires_and_suppresses ────────────────────────────
# ONE parametrised test over the suppressor set, so the negative can only pass
# when the positive fires.


@pytest.mark.parametrize(
    "make_ast,expect_notice",
    [
        (lambda w: _q(dimensions=[Dimension.CATEGORY], filters=[_transfer_only()]), True),
        (lambda w: _q(dimensions=[Dimension.ACCOUNT_TYPE], filters=[_transfer_only()]), True),
        (lambda w: _q(filters=[_transfer_only()]), True),  # no dimension
        (lambda w: _q(dimensions=[Dimension.ACCOUNT], filters=[_transfer_only()]), False),
        (lambda w: _q(dimensions=[Dimension.TXN_TYPE], filters=[_transfer_only()]), False),
        (
            lambda w: _q(filters=[_transfer_only(), _account_id(w["checking"].id)]),
            False,
        ),
    ],
    ids=[
        "dim=category:present",
        "dim=account_type:present",
        "no-dim:present",
        "dim=account:absent",
        "dim=txn_type:absent",
        "account_id-filtered:absent",
    ],
)
def test_transfer_notice_fires_and_suppresses(world, make_ast, expect_notice):
    ast = make_ast(world)
    got = _transfer_notice(ast)
    if expect_notice:
        assert got == _TRANSFER_NOTICE
    else:
        assert got is None


# ── fence: transfer_notice_covers_include_state ─────────────────────────────


def test_transfer_notice_covers_include_state(world):
    """State 2 (``include_non_reportable``) promotes the base identically to
    state 3 (``transfer=true``) and must fire the same notice."""
    ast = _q(dimensions=[Dimension.CATEGORY], include_non_reportable=True)
    assert _transfer_notice(ast) == _TRANSFER_NOTICE


def test_transfer_notice_absent_for_unpromoted_query(world):
    """Control: neither state active -> no notice, whatever the dimension."""
    ast = _q(dimensions=[Dimension.CATEGORY])
    assert _transfer_notice(ast) is None


# ── fence: transfer_notice_alone_on_single_currency_org ─────────────────────


async def test_transfer_notice_alone_on_single_currency_org(db, world):
    """THE case revision 1's compose fence could not see: an ordinary
    single-currency org has ``excluded_account_count == 0``, so
    ``_currency_notice`` returns ``None`` here -- a join written
    ``f"{a} {b}" if a and b else a`` silently drops the transfer sentence for
    essentially every real org."""
    ast = _q(dimensions=[Dimension.CATEGORY], filters=[_transfer_only()])
    _rows, meta = await reports_query_service.execute_query(
        db, ast, org_id=world["org"].id
    )
    assert meta["warning"] == _TRANSFER_NOTICE


# ── fence: transfer_notice_composes_with_currency ───────────────────────────


async def test_transfer_notice_composes_with_currency(db, multi_currency_world):
    """Multi-currency org (non-NULL ``primary_currency`` plus a second-currency
    account) gets BOTH exact sentences as substrings, plus a control that
    each half is non-empty alone.

    ⚠ Do NOT assert ``"and" in warning`` -- ``_TRANSFER_NOTICE`` itself
    contains "totals and counts", which would pass a transfer-only clobber."""
    w = multi_currency_world
    ast = _q(dimensions=[Dimension.CATEGORY], filters=[_transfer_only()])
    _rows, meta = await reports_query_service.execute_query(
        db, ast, org_id=w["org"].id
    )
    warning = meta["warning"]
    assert warning is not None
    assert _TRANSFER_NOTICE in warning
    assert "Multiple currencies held" in warning

    # Control: the currency half fires ALONE when transfers are not asked for
    # (the sibling single-currency-org test proves the transfer half fires
    # alone on its side -- an org with a second currency always resolves
    # ``_SCOPED`` when nothing mentions currency, so it cannot isolate the
    # transfer half by itself).
    currency_only_ast = _q(dimensions=[Dimension.CATEGORY])
    _rows_c, meta_c = await reports_query_service.execute_query(
        db, currency_only_ast, org_id=w["org"].id
    )
    assert meta_c["warning"] is not None and "Multiple currencies held" in meta_c["warning"]
    assert _TRANSFER_NOTICE not in meta_c["warning"]
