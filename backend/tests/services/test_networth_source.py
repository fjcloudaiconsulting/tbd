"""NetWorthSource — cumulative net worth over time, per currency.

Self-contained in-memory aiosqlite fixture (mirrors test_accounts_source.py).
Reconstruction = opening_balance (dated event) + signed settled deltas,
cumulative per currency, cash-basis.
"""
import pytest
import pytest_asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization
from app.models.category import CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
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

SRC = registry.get_source("networth")

_ORG_CAT: dict[int, int] = {}  # org_id -> a category id (transactions.category_id is NOT NULL)


def _q(dimensions=None, filters=None, limit=100):
    return ReportsQuery(
        dataset=Dataset.NETWORTH,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.NET_WORTH),
        dimensions=dimensions or [],
        filters=filters or [],
        limit=limit,
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


async def _org(db, name="Org"):
    o = Organization(name=name, billing_cycle_day=1)
    db.add(o)
    await db.flush()
    at = AccountType(org_id=o.id, name="Bank", slug="checking", is_system=True)
    cat = Category(org_id=o.id, name="Misc", type=CategoryType.EXPENSE)
    db.add_all([at, cat])
    await db.flush()
    _ORG_CAT[o.id] = cat.id
    return o, at


async def _acct(db, org, at, name, opening, opening_date, currency="EUR", balance=None):
    a = Account(
        org_id=org.id, name=name, account_type_id=at.id,
        balance=Decimal(str(balance if balance is not None else opening)),
        currency=currency, is_active=True,
        opening_balance=Decimal(str(opening)), opening_balance_date=opening_date,
    )
    db.add(a)
    await db.flush()
    return a


async def _tx(db, org, acct, amount, ttype, settled_date, *, txn_date=None,
              status=TransactionStatus.SETTLED, linked_id=None, manual=False):
    t = Transaction(
        org_id=org.id, account_id=acct.id, category_id=_ORG_CAT[org.id],
        description="tx", amount=Decimal(str(amount)),
        type=ttype, status=status, date=txn_date or settled_date,
        settled_date=settled_date, linked_transaction_id=linked_id,
        is_manual_adjustment=manual,
    )
    db.add(t)
    await db.flush()
    return t


# ─── reconstruction ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_month_series_reconstruction(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "Chk", 1000, date(2026, 1, 5))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 10))
    await _tx(db_session, org, a, 200, TransactionType.EXPENSE, date(2026, 3, 3))

    rows, meta = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert series == {"2026-01": 1000.0, "2026-02": 1500.0, "2026-03": 1300.0}
    assert meta.get("warning") is None


@pytest.mark.asyncio
async def test_reconciles_to_balance(db_session):
    # balance the app would maintain = opening + signed settled deltas
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "Chk", 1000, date(2026, 1, 5), balance=1300)
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 10))
    await _tx(db_session, org, a, 200, TransactionType.EXPENSE, date(2026, 3, 3))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    latest = rows[-1]["value"]
    assert Decimal(str(latest)) == a.balance  # 1300


@pytest.mark.asyncio
async def test_cash_basis_uses_settled_date(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "Chk", 0, date(2026, 1, 1))
    # dated Jan, settled Feb -> counts in Feb
    await _tx(db_session, org, a, 100, TransactionType.INCOME,
              date(2026, 2, 15), txn_date=date(2026, 1, 20))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert "2026-01" in series and series["2026-01"] == 0.0
    assert series["2026-02"] == 100.0


@pytest.mark.asyncio
async def test_mid_history_opening(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 100, date(2026, 1, 1))
    await _tx(db_session, org, a, 50, TransactionType.INCOME, date(2026, 2, 1))
    # B opened in March -> steps up only from March
    await _acct(db_session, org, at, "B", 5000, date(2026, 3, 1))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert series == {"2026-01": 100.0, "2026-02": 150.0, "2026-03": 5150.0}


# ─── exclusions ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_excluded(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 0, date(2026, 1, 1))
    await _tx(db_session, org, a, 100, TransactionType.INCOME, date(2026, 2, 1),
              status=TransactionStatus.PENDING)
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert series.get("2026-02", 0.0) == 0.0


@pytest.mark.asyncio
async def test_transfer_pair_nets_zero(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    b = await _acct(db_session, org, at, "B", 0, date(2026, 1, 1))
    # transfer 300 A->B in Feb: legs typed by direction, reciprocal-linked
    out_leg = await _tx(db_session, org, a, 300, TransactionType.EXPENSE, date(2026, 2, 1))
    in_leg = await _tx(db_session, org, b, 300, TransactionType.INCOME, date(2026, 2, 1),
                       linked_id=out_leg.id)
    out_leg.linked_transaction_id = in_leg.id  # reciprocal
    await db_session.flush()
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    # total net worth (1000) is unchanged by the transfer
    assert series["2026-02"] == 1000.0


@pytest.mark.asyncio
async def test_manual_adjustment_counted(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 0, date(2026, 1, 1))
    await _tx(db_session, org, a, 250, TransactionType.INCOME, date(2026, 2, 1), manual=True)
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert series["2026-02"] == 250.0  # counted (moved the real balance)


# ─── multi-currency ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_currency_no_dim_warns_and_never_sums(db_session):
    org, at = await _org(db_session)
    await _acct(db_session, org, at, "EUR", 1000, date(2026, 1, 1), currency="EUR")
    await _acct(db_session, org, at, "USD", 2000, date(2026, 1, 1), currency="USD")
    rows, meta = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    # per-currency rows, never a 3000 sum
    by = {(r["month"], r["currency"]): r["value"] for r in rows}
    assert by[("2026-01", "EUR")] == 1000.0
    assert by[("2026-01", "USD")] == 2000.0
    assert meta.get("warning")


@pytest.mark.asyncio
async def test_currency_filter_narrows(db_session):
    org, at = await _org(db_session)
    await _acct(db_session, org, at, "EUR", 1000, date(2026, 1, 1), currency="EUR")
    await _acct(db_session, org, at, "USD", 2000, date(2026, 1, 1), currency="USD")
    q = _q([Dimension.MONTH], [Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="EUR")])
    rows, meta = await SRC.build_rows(db_session, org.id, q)
    assert all(r.get("currency", "EUR") == "EUR" for r in rows)
    assert rows[0]["value"] == 1000.0
    assert meta.get("warning") is None


@pytest.mark.asyncio
async def test_single_currency_no_dim_drops_currency_key(db_session):
    org, at = await _org(db_session)
    await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1), currency="EUR")
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    assert "currency" not in rows[0]  # clean single series


# ─── windowing ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_date_between_carries_prior_history(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 1))
    await _tx(db_session, org, a, 300, TransactionType.INCOME, date(2026, 3, 1))
    q = _q([Dimension.MONTH], [Filter(
        field=FilterField.DATE, op=FilterOp.BETWEEN,
        value=[date(2026, 2, 1), date(2026, 3, 31)])])
    rows, _ = await SRC.build_rows(db_session, org.id, q)
    series = {r["month"]: r["value"] for r in rows}
    # first visible period (Feb) carries the Jan opening (1000) + Feb (500) = 1500
    assert "2026-01" not in series
    assert series["2026-02"] == 1500.0
    assert series["2026-03"] == 1800.0


@pytest.mark.asyncio
async def test_date_lte_as_of_cutoff(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 1))
    await _tx(db_session, org, a, 300, TransactionType.INCOME, date(2026, 3, 1))
    q = _q([Dimension.MONTH], [Filter(
        field=FilterField.DATE, op=FilterOp.LTE, value=date(2026, 2, 28))])
    rows, _ = await SRC.build_rows(db_session, org.id, q)
    series = {r["month"]: r["value"] for r in rows}
    assert "2026-03" not in series
    assert series["2026-02"] == 1500.0


@pytest.mark.asyncio
async def test_date_gte_start_only_slices(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 1))
    q = _q([Dimension.MONTH], [Filter(
        field=FilterField.DATE, op=FilterOp.GTE, value=date(2026, 2, 1))])
    rows, _ = await SRC.build_rows(db_session, org.id, q)
    series = {r["month"]: r["value"] for r in rows}
    assert "2026-01" not in series
    assert series["2026-02"] == 1500.0  # carries the Jan opening


# ─── KPI (no dimension) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kpi_no_dim_latest_total(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 1))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([], limit=1))
    assert len(rows) == 1
    assert rows[0]["value"] == 1500.0


@pytest.mark.asyncio
async def test_by_currency_dimension_point_in_time(db_session):
    org, at = await _org(db_session)
    await _acct(db_session, org, at, "E", 1000, date(2026, 1, 1), currency="EUR")
    await _acct(db_session, org, at, "U", 2000, date(2026, 1, 1), currency="USD")
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.CURRENCY]))
    by = {r["currency"]: r["value"] for r in rows}
    assert by == {"EUR": 1000.0, "USD": 2000.0}


# ─── validate + registry + isolation ────────────────────────────────


def test_validate_rejects_wrong_measure_and_dim():
    with pytest.raises(ValueError):
        SRC.validate(ReportsQuery(
            dataset=Dataset.NETWORTH,
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        ))
    with pytest.raises(ValueError):
        SRC.validate(_q([Dimension.CATEGORY]))


def test_validate_accepts_published_shape():
    SRC.validate(_q([Dimension.MONTH, Dimension.CURRENCY], [
        Filter(field=FilterField.DATE, op=FilterOp.GTE, value="2026-01-01"),
        Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"]),
    ]))


def test_registered_with_catalog():
    assert SRC.key == "networth"
    assert {m.field for m in SRC.measures()} == {"net_worth"}
    assert {d.key for d in SRC.dimensions()} == {"month", "week", "day", "currency"}
    assert {f.field for f in SRC.filters()} == {"currency", "account_id", "date"}


@pytest.mark.asyncio
async def test_org_isolation(db_session):
    org1, at1 = await _org(db_session, "One")
    org2, at2 = await _org(db_session, "Two")
    await _acct(db_session, org1, at1, "A", 1000, date(2026, 1, 1))
    await _acct(db_session, org2, at2, "B", 9999, date(2026, 1, 1))
    rows, _ = await SRC.build_rows(db_session, org1.id, _q([Dimension.MONTH]))
    assert all(r["value"] == 1000.0 for r in rows)


@pytest.mark.asyncio
async def test_foreign_account_id_returns_empty(db_session):
    """Adversarial: an org1 caller smuggling org2's account_id sees nothing —
    the org gate on BOTH streams (not the account filter) prevents the leak."""
    org1, at1 = await _org(db_session, "One")
    org2, at2 = await _org(db_session, "Two")
    other = await _acct(db_session, org2, at2, "Foreign", 5000, date(2026, 1, 1))
    q = _q([Dimension.MONTH], [Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.IN, value=[other.id])])
    rows, _ = await SRC.build_rows(db_session, org1.id, q)
    assert rows == []


# ─── granularity + gap + edge coverage ──────────────────────────────


@pytest.mark.asyncio
async def test_day_granularity(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 5))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 1, 10))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.DAY]))
    series = {r["day"]: r["value"] for r in rows}
    assert series == {"2026-01-05": 1000.0, "2026-01-10": 1500.0}


@pytest.mark.asyncio
async def test_week_granularity(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 5))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 3, 10))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.WEEK]))
    # two distinct weeks, chronological, cumulative
    assert [r["value"] for r in rows] == [1000.0, 1500.0]
    assert all("week" in r for r in rows)


@pytest.mark.asyncio
async def test_gap_carry_forward(db_session):
    org, at = await _org(db_session)
    a = await _acct(db_session, org, at, "A", 1000, date(2026, 1, 1))
    await _tx(db_session, org, a, 500, TransactionType.INCOME, date(2026, 2, 1))
    # March has no activity → no point emitted (sparse); April carries Feb total
    await _tx(db_session, org, a, 300, TransactionType.EXPENSE, date(2026, 4, 1))
    rows, _ = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    series = {r["month"]: r["value"] for r in rows}
    assert series == {"2026-01": 1000.0, "2026-02": 1500.0, "2026-04": 1200.0}
    assert "2026-03" not in series


@pytest.mark.asyncio
async def test_multi_currency_kpi_truncates_and_warns(db_session):
    org, at = await _org(db_session)
    await _acct(db_session, org, at, "E", 1000, date(2026, 1, 1), currency="EUR")
    await _acct(db_session, org, at, "U", 2000, date(2026, 1, 1), currency="USD")
    rows, meta = await SRC.build_rows(db_session, org.id, _q([], limit=1))
    assert len(rows) == 1  # limit:1 keeps one currency (never a summed 3000)
    assert rows[0]["value"] in (1000.0, 2000.0)
    assert meta.get("warning")
    assert meta["truncated"] is True


@pytest.mark.asyncio
async def test_zero_accounts_empty(db_session):
    org, _at = await _org(db_session)
    rows, meta = await SRC.build_rows(db_session, org.id, _q([Dimension.MONTH]))
    assert rows == []
    assert meta["row_count"] == 0
    assert meta["truncated"] is False
