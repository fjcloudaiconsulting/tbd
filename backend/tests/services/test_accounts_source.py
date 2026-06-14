"""AccountsSource — balance/count over the accounts snapshot.

Self-contained in-memory aiosqlite fixture (mirrors
test_transaction_service_pair.py): engine + PRAGMA foreign_keys=ON +
StaticPool. Does NOT rely on conftest fixtures.
"""
import pytest
import pytest_asyncio
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Organization
from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation,
    Dataset,
    Dimension,
    Filter,
    FilterField,
    FilterOp,
    Measure,
    MeasureField,
    ReportsQuery,
)


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
async def seeded(db_session):
    """Org 1: Bank type (Checking 500, Savings 1500, both active/EUR),
    Card type (OldCard -200, inactive/EUR). Org 2: one account for
    org-isolation."""
    org1 = Organization(name="Org1", billing_cycle_day=1)
    org2 = Organization(name="Org2", billing_cycle_day=1)
    db_session.add_all([org1, org2])
    await db_session.flush()

    bank = AccountType(org_id=org1.id, name="Bank", slug="bank", is_system=False)
    card = AccountType(org_id=org1.id, name="Card", slug="card", is_system=False)
    other_at = AccountType(org_id=org2.id, name="Other", slug="other", is_system=False)
    db_session.add_all([bank, card, other_at])
    await db_session.flush()

    checking = Account(
        org_id=org1.id, name="Checking", account_type_id=bank.id,
        balance=Decimal("500"), currency="EUR", is_active=True,
    )
    savings = Account(
        org_id=org1.id, name="Savings", account_type_id=bank.id,
        balance=Decimal("1500"), currency="EUR", is_active=True,
    )
    oldcard = Account(
        org_id=org1.id, name="OldCard", account_type_id=card.id,
        balance=Decimal("-200"), currency="EUR", is_active=False,
    )
    other = Account(
        org_id=org2.id, name="OtherAcct", account_type_id=other_at.id,
        balance=Decimal("999"), currency="EUR", is_active=True,
    )
    db_session.add_all([checking, savings, oldcard, other])
    await db_session.flush()

    return {"org1_id": org1.id, "org2_id": org2.id}


def _source():
    return registry.get_source("accounts")


def _query(**kwargs):
    base = dict(
        dataset=Dataset.ACCOUNTS,
        measure=Measure(agg=Aggregation.COUNT, field=MeasureField.ID),
    )
    base.update(kwargs)
    return ReportsQuery(**base)


@pytest.mark.asyncio
async def test_sum_balance_by_account_type(db_session, seeded):
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[Dimension.ACCOUNT_TYPE],
    )
    rows, meta = await src.build_rows(db_session, seeded["org1_id"], ast)
    by_type = {r["account_type"]: r["value"] for r in rows}
    assert by_type == {"Bank": 2000.0, "Card": -200.0}
    assert meta["row_count"] == 2


@pytest.mark.asyncio
async def test_count_by_account_active(db_session, seeded):
    src = _source()
    ast = _query(dimensions=[Dimension.ACCOUNT_ACTIVE])
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    by_active = {r["account_active"]: r["value"] for r in rows}
    assert by_active == {"Active": 2, "Inactive": 1}


@pytest.mark.asyncio
async def test_count_no_dims_org_isolation(db_session, seeded):
    src = _source()
    ast = _query()
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 3  # org2's account excluded


@pytest.mark.asyncio
async def test_date_filter_tolerated_and_dropped(db_session, seeded):
    """A date filter against accounts must validate() without raising and
    build_rows must return rows (date dropped) — the Phase-5 shared-canvas
    bar contract."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.DATE, op=FilterOp.BETWEEN,
                   value=["2024-01-01", "2026-01-01"]),
        ],
    )
    src.validate(ast)  # must not raise
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 3


@pytest.mark.asyncio
async def test_sum_balance_by_currency_with_currency_filter(db_session, seeded):
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[Dimension.CURRENCY],
        filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="EUR")],
    )
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert {r["currency"] for r in rows} == {"EUR"}
    assert rows[0]["value"] == 1800.0  # 500 + 1500 - 200, all EUR


@pytest.mark.asyncio
async def test_validate_rejects_sum_amount(db_session, seeded):
    src = _source()
    ast = _query(measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT))
    with pytest.raises(ValueError):
        src.validate(ast)


@pytest.mark.asyncio
async def test_validate_rejects_category_dimension(db_session, seeded):
    src = _source()
    ast = _query(dimensions=[Dimension.CATEGORY])
    with pytest.raises(ValueError):
        src.validate(ast)
