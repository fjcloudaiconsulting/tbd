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
    SortBy,
    SortDir,
    SortSpec,
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
async def test_status_filter_tolerated_and_dropped(db_session, seeded):
    """A canvas-level status filter cascaded onto accounts must validate()
    without raising and build_rows must still return rows (status dropped) —
    the shared-canvas contract, now extended to ``status`` so it can cascade
    beyond transactions widgets."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.STATUS, op=FilterOp.EQ, value="settled"),
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


def test_balance_between_filter_constructs():
    """The AST Filter validator must accept ``balance BETWEEN``. Also
    confirms scalar ``gte``/``lte`` on balance still construct."""
    # BETWEEN — was rejected with 422 before the validator fix.
    f = Filter(
        field=FilterField.BALANCE,
        op=FilterOp.BETWEEN,
        value=[Decimal("0"), Decimal("1000")],
    )
    assert f.value == [Decimal("0"), Decimal("1000")]

    # Scalar range ops are unaffected and should still construct.
    Filter(field=FilterField.BALANCE, op=FilterOp.GTE, value=Decimal("0"))
    Filter(field=FilterField.BALANCE, op=FilterOp.LTE, value=Decimal("1000"))


@pytest.mark.asyncio
async def test_balance_between_filter_applied(db_session, seeded):
    """End-to-end: ``balance BETWEEN [0, 1000]`` validates and only counts
    accounts in range (Checking 500 -> 1; Savings 1500 and OldCard -200
    excluded)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.BALANCE, op=FilterOp.BETWEEN, value=[0, 1000]),
        ],
    )
    src.validate(ast)  # must not raise
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 1


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


# ─── helpers for IN / type filters ──────────────────────────────────


async def _ids(db_session, org_id):
    """Return {name -> account.id} and {slug -> account_type.id} for org1."""
    from sqlalchemy import select as _select

    acct_rows = (
        await db_session.execute(
            _select(Account.name, Account.id).where(Account.org_id == org_id)
        )
    ).all()
    type_rows = (
        await db_session.execute(
            _select(AccountType.slug, AccountType.id).where(
                AccountType.org_id == org_id
            )
        )
    ).all()
    return {n: i for n, i in acct_rows}, {s: i for s, i in type_rows}


# ─── Fix 1: validate enforces catalog filter OPS ────────────────────


@pytest.mark.asyncio
async def test_validate_rejects_balance_eq_op(db_session, seeded):
    """balance is published but only supports between/gte/lte. An `eq`
    op must be rejected by validate() (→422), not silently dropped by the
    BALANCE branch of _apply_filter."""
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.BALANCE, op=FilterOp.EQ, value=100)],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


@pytest.mark.asyncio
async def test_validate_rejects_account_active_in_op(db_session, seeded):
    """account_active is published but only supports eq. An `in` op must
    be rejected (it would otherwise become a constant-TRUE predicate via
    bool([False]))."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.ACCOUNT_ACTIVE, op=FilterOp.IN, value=[False]),
        ],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


@pytest.mark.asyncio
async def test_validate_rejects_non_shared_field(db_session, seeded):
    """A transactions-only field (txn_type) that accounts does not publish
    and is not a shared-canvas field must be rejected outright. (``status``
    used to be this example but is now a shared-canvas field — see
    ``test_status_filter_tolerated_and_dropped``.)"""
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.TXN_TYPE, op=FilterOp.EQ, value="expense")],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


# ─── Fix 2 / finding 6: account_active eq filter ────────────────────


@pytest.mark.asyncio
async def test_account_active_eq_false_filter(db_session, seeded):
    """account_active eq false returns only the inactive account (OldCard)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.ACCOUNT_ACTIVE, op=FilterOp.EQ, value=False),
        ],
    )
    src.validate(ast)  # must not raise
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 1  # only OldCard is inactive


# ─── finding 7: avg + IN filters ────────────────────────────────────


@pytest.mark.asyncio
async def test_avg_balance_over_bank_accounts(db_session, seeded):
    """avg over the two Bank accounts (Checking 500 + Savings 1500) = 1000.0."""
    src = _source()
    _, types = await _ids(db_session, seeded["org1_id"])
    ast = _query(
        measure=Measure(agg=Aggregation.AVG, field=MeasureField.BALANCE),
        filters=[
            Filter(
                field=FilterField.ACCOUNT_TYPE,
                op=FilterOp.EQ,
                value=types["bank"],
            ),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 1000.0


@pytest.mark.asyncio
async def test_account_id_in_filter(db_session, seeded):
    src = _source()
    accts, _ = await _ids(db_session, seeded["org1_id"])
    ast = _query(
        filters=[
            Filter(
                field=FilterField.ACCOUNT_ID,
                op=FilterOp.IN,
                value=[accts["Checking"], accts["Savings"]],
            ),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 2


@pytest.mark.asyncio
async def test_currency_in_filter(db_session, seeded):
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"]),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 3  # all three org1 accounts are EUR


@pytest.mark.asyncio
async def test_account_type_in_filter(db_session, seeded):
    src = _source()
    _, types = await _ids(db_session, seeded["org1_id"])
    ast = _query(
        filters=[
            Filter(
                field=FilterField.ACCOUNT_TYPE,
                op=FilterOp.IN,
                value=[types["bank"]],
            ),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 2  # Checking + Savings are Bank


# ─── Fix 3: honor query.sort + stable tiebreaker ────────────────────


@pytest.mark.asyncio
async def test_sort_by_value_asc(db_session, seeded):
    """Grouped query (dim=account) sorted ascending by value returns rows
    ordered low → high balance."""
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
        dimensions=[Dimension.ACCOUNT],
        sort=SortSpec(by=SortBy.VALUE, dir=SortDir.ASC),
    )
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    values = [r["value"] for r in rows]
    assert values == sorted(values)
    # OldCard (-200) first, Savings (1500) last.
    assert rows[0]["account"] == "OldCard"
    assert rows[-1]["account"] == "Savings"


@pytest.mark.asyncio
async def test_sort_truncation_is_deterministic(db_session, seeded):
    """With a small limit, the truncated set is identical across runs
    (stable tiebreaker)."""
    src = _source()

    def _ast():
        return _query(
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
            dimensions=[Dimension.ACCOUNT],
            sort=SortSpec(by=SortBy.VALUE, dir=SortDir.ASC),
            limit=2,
        )

    rows_a, _ = await src.build_rows(db_session, seeded["org1_id"], _ast())
    rows_b, _ = await src.build_rows(db_session, seeded["org1_id"], _ast())
    assert len(rows_a) == 2
    assert [r["account"] for r in rows_a] == [r["account"] for r in rows_b]
