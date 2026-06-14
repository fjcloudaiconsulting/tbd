"""RecurringSource — amount/count over recurring templates.

Self-contained in-memory aiosqlite fixture (mirrors
test_accounts_source.py): engine + PRAGMA foreign_keys=ON + StaticPool.
Does NOT rely on conftest fixtures.
"""
import pytest
import pytest_asyncio
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models import Account, AccountType, Category, Organization
from app.models.recurring import RecurringTransaction
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
    """Org 1: one EUR account + categories + 4 recurring templates:
      Rent  (expense, monthly,  1200, active)
      Salary(income,  monthly,  3000, active)
      Gym   (expense, weekly,     30, active)
      OldSub(expense, monthly,    10, INACTIVE)
    Org 2: one recurring row for org-isolation.
    """
    from datetime import date

    org1 = Organization(name="Org1", billing_cycle_day=1)
    org2 = Organization(name="Org2", billing_cycle_day=1)
    db_session.add_all([org1, org2])
    await db_session.flush()

    at1 = AccountType(org_id=org1.id, name="Bank", slug="bank", is_system=False)
    at2 = AccountType(org_id=org2.id, name="Other", slug="other", is_system=False)
    db_session.add_all([at1, at2])
    await db_session.flush()

    acct1 = Account(
        org_id=org1.id, name="Checking", account_type_id=at1.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    acct2 = Account(
        org_id=org2.id, name="OtherAcct", account_type_id=at2.id,
        balance=Decimal("0"), currency="EUR", is_active=True,
    )
    db_session.add_all([acct1, acct2])
    await db_session.flush()

    cat_housing = Category(org_id=org1.id, name="Housing", type="expense")
    cat_income = Category(org_id=org1.id, name="Income", type="income")
    cat_health = Category(org_id=org1.id, name="Health", type="expense")
    cat_other = Category(org_id=org2.id, name="OtherCat", type="expense")
    db_session.add_all([cat_housing, cat_income, cat_health, cat_other])
    await db_session.flush()

    rows = [
        RecurringTransaction(
            org_id=org1.id, account_id=acct1.id, category_id=cat_housing.id,
            description="Rent", amount=Decimal("1200"), type="expense",
            frequency="monthly", next_due_date=date(2026, 1, 1), is_active=True,
        ),
        RecurringTransaction(
            org_id=org1.id, account_id=acct1.id, category_id=cat_income.id,
            description="Salary", amount=Decimal("3000"), type="income",
            frequency="monthly", next_due_date=date(2026, 1, 1), is_active=True,
        ),
        RecurringTransaction(
            org_id=org1.id, account_id=acct1.id, category_id=cat_health.id,
            description="Gym", amount=Decimal("30"), type="expense",
            frequency="weekly", next_due_date=date(2026, 1, 1), is_active=True,
        ),
        RecurringTransaction(
            org_id=org1.id, account_id=acct1.id, category_id=cat_housing.id,
            description="OldSub", amount=Decimal("10"), type="expense",
            frequency="monthly", next_due_date=date(2026, 1, 1), is_active=False,
        ),
        RecurringTransaction(
            org_id=org2.id, account_id=acct2.id, category_id=cat_other.id,
            description="OtherOrg", amount=Decimal("99"), type="expense",
            frequency="yearly", next_due_date=date(2026, 1, 1), is_active=True,
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()

    return {"org1_id": org1.id, "org2_id": org2.id}


def _source():
    return registry.get_source("recurring")


def _query(**kwargs):
    base = dict(
        dataset=Dataset.RECURRING,
        measure=Measure(agg=Aggregation.COUNT, field=MeasureField.ID),
    )
    base.update(kwargs)
    return ReportsQuery(**base)


async def _ids(db_session, org_id):
    """Return {name -> account.id} and {name -> category.id} for the org."""
    from sqlalchemy import select as _select

    acct_rows = (
        await db_session.execute(
            _select(Account.name, Account.id).where(Account.org_id == org_id)
        )
    ).all()
    cat_rows = (
        await db_session.execute(
            _select(Category.name, Category.id).where(Category.org_id == org_id)
        )
    ).all()
    return {n: i for n, i in acct_rows}, {n: i for n, i in cat_rows}


# ─── catalog exactness ──────────────────────────────────────────────


def test_catalog_exactness():
    src = _source()
    assert src.key == "recurring"
    assert src.label == "Recurring"
    assert {d.key for d in src.dimensions()} == {
        "category", "account", "currency", "txn_type", "frequency",
        "recurring_active",
    }
    assert {m.key for m in src.measures()} == {
        "sum_amount", "avg_amount", "count_recurring",
    }
    assert {f.field for f in src.filters()} == {
        "account_id", "category_id", "currency", "txn_type", "frequency",
        "recurring_active", "amount",
    }


# ─── grouped aggregates ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sum_amount_by_txn_type(db_session, seeded):
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.TXN_TYPE],
    )
    rows, meta = await src.build_rows(db_session, seeded["org1_id"], ast)
    by_type = {r["txn_type"]: r["value"] for r in rows}
    # expense: 1200 + 30 + 10 = 1240; income: 3000
    assert by_type == {"expense": 1240.0, "income": 3000.0}
    assert meta["row_count"] == 2


@pytest.mark.asyncio
async def test_sum_amount_by_frequency(db_session, seeded):
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.FREQUENCY],
    )
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    by_freq = {r["frequency"]: r["value"] for r in rows}
    # monthly: Rent 1200 + Salary 3000 + OldSub 10 = 4210; weekly: Gym 30
    assert by_freq == {"monthly": 4210.0, "weekly": 30.0}


@pytest.mark.asyncio
async def test_count_by_recurring_active(db_session, seeded):
    src = _source()
    ast = _query(dimensions=[Dimension.RECURRING_ACTIVE])
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    by_active = {r["recurring_active"]: r["value"] for r in rows}
    assert by_active == {"Active": 3, "Inactive": 1}


@pytest.mark.asyncio
async def test_count_no_dims_org_isolation(db_session, seeded):
    src = _source()
    ast = _query()
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 4  # org2's recurring row excluded


@pytest.mark.asyncio
async def test_frequency_dimension_keys_are_plain_strings(db_session, seeded):
    """Grouped row keys for the frequency dimension must be plain JSON
    strings ("monthly"), never a Python Frequency enum repr."""
    src = _source()
    ast = _query(dimensions=[Dimension.FREQUENCY])
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    keys = {r["frequency"] for r in rows}
    assert keys == {"monthly", "weekly"}
    for r in rows:
        assert type(r["frequency"]) is str


@pytest.mark.asyncio
async def test_txn_type_dimension_keys_are_plain_strings(db_session, seeded):
    src = _source()
    ast = _query(dimensions=[Dimension.TXN_TYPE])
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    for r in rows:
        assert type(r["txn_type"]) is str
    # Verify the actual key values, not just their type: the seed has
    # expense + income templates (recurring never has transfer).
    assert {r["txn_type"] for r in rows} == {"income", "expense"}


# ─── avg + IN filters ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_avg_amount_over_monthly_expense(db_session, seeded):
    """avg over the two monthly expense rows (Rent 1200 + OldSub 10) = 605."""
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.AVG, field=MeasureField.AMOUNT),
        filters=[
            Filter(field=FilterField.TXN_TYPE, op=FilterOp.EQ, value="expense"),
            Filter(field=FilterField.FREQUENCY, op=FilterOp.EQ, value="monthly"),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 605.0


@pytest.mark.asyncio
async def test_account_id_in_filter(db_session, seeded):
    src = _source()
    accts, _ = await _ids(db_session, seeded["org1_id"])
    ast = _query(
        filters=[
            Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.IN,
                   value=[accts["Checking"]]),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 4


@pytest.mark.asyncio
async def test_category_id_in_filter(db_session, seeded):
    src = _source()
    _, cats = await _ids(db_session, seeded["org1_id"])
    ast = _query(
        filters=[
            Filter(field=FilterField.CATEGORY_ID, op=FilterOp.IN,
                   value=[cats["Housing"]]),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 2  # Rent + OldSub are Housing


@pytest.mark.asyncio
async def test_currency_in_filter(db_session, seeded):
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"])],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 4  # all org1 recurring rows joined to EUR account


@pytest.mark.asyncio
async def test_txn_type_in_filter(db_session, seeded):
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.TXN_TYPE, op=FilterOp.IN, value=["income"])],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 1  # only Salary


@pytest.mark.asyncio
async def test_frequency_in_filter(db_session, seeded):
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.FREQUENCY, op=FilterOp.IN,
                        value=["weekly"])],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert rows[0]["value"] == 1  # only Gym


@pytest.mark.asyncio
async def test_recurring_active_eq_false_filter(db_session, seeded):
    """recurring_active eq false returns only the inactive template (OldSub)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.RECURRING_ACTIVE, op=FilterOp.EQ, value=False),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 1


@pytest.mark.asyncio
async def test_amount_between_filter(db_session, seeded):
    """amount BETWEEN [0, 100] counts only Gym (30) + OldSub (10)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.AMOUNT, op=FilterOp.BETWEEN, value=[0, 100]),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 2


@pytest.mark.asyncio
async def test_amount_gte_filter(db_session, seeded):
    """amount GTE 30 counts Rent (1200) + Salary (3000) + Gym (30) = 3,
    excluding OldSub (10). Threshold 30 pins the comparison direction: a
    swapped `<= 30` would yield only {30, 10} = 2, so 3 != 2 catches a
    gte/lte swap on the gte branch."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.AMOUNT, op=FilterOp.GTE, value=30),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 3


@pytest.mark.asyncio
async def test_amount_lte_filter(db_session, seeded):
    """amount LTE 30 counts only Gym (30) + OldSub (10). Pins the comparison
    direction (<= not >=) on the lte branch, and the boundary (30 inclusive)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.AMOUNT, op=FilterOp.LTE, value=30),
        ],
    )
    src.validate(ast)
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    assert len(rows) == 1
    assert rows[0]["value"] == 2


# ─── op-reject (→422) ───────────────────────────────────────────────


def test_validate_rejects_frequency_unsupported_scalar_op():
    """frequency only supports eq/in. A schema-valid scalar op the catalog
    does not list (gte) must be rejected."""
    src = _source()
    # `between` is only schema-valid on date/amount/balance, so we can't use
    # it here; use `gte` (a schema-allowed scalar op) which the frequency
    # catalog (eq/in) does not list, and assert the validator rejects it.
    ast = _query(
        filters=[Filter(field=FilterField.FREQUENCY, op=FilterOp.GTE, value="monthly")],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


def test_validate_rejects_recurring_active_in_op():
    """recurring_active only supports eq. An `in` op must be rejected."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.RECURRING_ACTIVE, op=FilterOp.IN, value=[False]),
        ],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


# ─── Fix 1: txn_type='transfer' is invalid for recurring ────────────


def test_validate_rejects_txn_type_transfer_eq():
    """RecurringTransaction.type is Enum(income, expense) — recurring has NO
    transfer. The shared scalar coercion accepts transfer (correct for
    transactions), so validate() must reject it here or it silently compiles
    to type=='transfer' (empty result on MySQL)."""
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.TXN_TYPE, op=FilterOp.EQ, value="transfer")],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


def test_validate_rejects_txn_type_transfer_in_list():
    """An `in` list containing transfer must also be rejected, even when it
    also lists a valid value (income)."""
    src = _source()
    ast = _query(
        filters=[
            Filter(field=FilterField.TXN_TYPE, op=FilterOp.IN,
                   value=["income", "transfer"]),
        ],
    )
    with pytest.raises(ValueError):
        src.validate(ast)


def test_validate_accepts_txn_type_income_eq():
    """Sanity: a valid recurring txn_type (income) must NOT raise."""
    src = _source()
    ast = _query(
        filters=[Filter(field=FilterField.TXN_TYPE, op=FilterOp.EQ, value="income")],
    )
    src.validate(ast)  # must not raise


# ─── cross-source reject ────────────────────────────────────────────


def test_transactions_source_rejects_frequency_dimension():
    """The frequency dimension is recurring-only; the transactions source
    must reject it."""
    txn = registry.get_source("transactions")
    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.FREQUENCY],
    )
    with pytest.raises(ValueError):
        txn.validate(ast)


def test_recurring_rejects_balance_measure_field():
    """sum(amount) is fine on recurring, but `balance` (an accounts field)
    is not a recurring measure field and must be rejected."""
    src = _source()
    ast = _query(measure=Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE))
    with pytest.raises(ValueError):
        src.validate(ast)


def test_recurring_accepts_sum_amount():
    src = _source()
    ast = _query(measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT))
    src.validate(ast)  # must not raise


# ─── date-drop tolerance (shared-canvas contract) ───────────────────


@pytest.mark.asyncio
async def test_date_filter_tolerated_and_dropped(db_session, seeded):
    """A date filter against recurring must validate() without raising and
    build_rows must return rows (date dropped)."""
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
    assert rows[0]["value"] == 4


# ─── sort + tiebreaker ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sort_by_value_asc(db_session, seeded):
    """Grouped query (dim=category) sorted ascending by value returns rows
    ordered low → high SUM(amount). Category sums over the seed:
    Health = Gym 30; Housing = Rent 1200 + OldSub 10 = 1210; Income = 3000."""
    src = _source()
    ast = _query(
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
        sort=SortSpec(by=SortBy.VALUE, dir=SortDir.ASC),
    )
    rows, _ = await src.build_rows(db_session, seeded["org1_id"], ast)
    values = [r["value"] for r in rows]
    assert values == sorted(values)
    # Boundary + value anchors (lowest / highest sum category).
    assert rows[0]["category"] == "Health"
    assert rows[-1]["category"] == "Income"
    # Concrete ordered values list (SUM(amount) is coerced to float).
    assert values == [30.0, 1210.0, 3000.0]


@pytest.mark.asyncio
async def test_sort_truncation_is_deterministic(db_session, seeded):
    """Engineer a real aggregate tie AT the truncation boundary: two NEW
    categories whose SUM(amount) is equal (each 5) and lower than every
    seeded category, so a limit-1 ASC sort must pick between the two tied
    rows. The func.min(id) tiebreaker guarantees the SAME single dimension
    value across runs; deleting that ORDER BY line makes this fail (MySQL is
    free to return either tied row)."""
    from decimal import Decimal as _Decimal
    from datetime import date as _date

    # Engineer the tie so the category NAME order opposes the row id /
    # insertion order: "Zeta" (inserted first → smaller min(id)) vs "Alpha"
    # (inserted second → larger min(id)). With the func.min(id) tiebreaker
    # the survivor is deterministic (smaller id, "Zeta"); without it the
    # engine is free to fall back to grouping/name order ("Alpha") — so
    # deleting the tiebreaker line flips the truncated value and fails.
    zeta = Category(org_id=seeded["org1_id"], name="Zeta", type="expense")
    alpha = Category(org_id=seeded["org1_id"], name="Alpha", type="expense")
    db_session.add(zeta)
    await db_session.flush()
    db_session.add(alpha)
    await db_session.flush()

    accts, _ = await _ids(db_session, seeded["org1_id"])
    checking = accts["Checking"]
    db_session.add_all([
        RecurringTransaction(
            org_id=seeded["org1_id"], account_id=checking, category_id=zeta.id,
            description="Zeta", amount=_Decimal("5"), type="expense",
            frequency="monthly", next_due_date=_date(2026, 1, 1), is_active=True,
        ),
        RecurringTransaction(
            org_id=seeded["org1_id"], account_id=checking, category_id=alpha.id,
            description="Alpha", amount=_Decimal("5"), type="expense",
            frequency="monthly", next_due_date=_date(2026, 1, 1), is_active=True,
        ),
    ])
    await db_session.flush()

    def _ast():
        return _query(
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
            dimensions=[Dimension.CATEGORY],
            sort=SortSpec(by=SortBy.VALUE, dir=SortDir.ASC),
            limit=1,
        )

    src = _source()
    rows_a, _ = await src.build_rows(db_session, seeded["org1_id"], _ast())
    rows_b, _ = await src.build_rows(db_session, seeded["org1_id"], _ast())
    assert len(rows_a) == 1
    assert rows_a[0]["value"] == 5.0  # the tied minimum is what gets truncated to
    # Stable across runs ...
    assert rows_a[0]["category"] == rows_b[0]["category"]
    # ... and anchored to the func.min(id) winner ("Zeta", inserted first =
    # smaller id), NOT the name-order fallback ("Alpha"). Removing the
    # tiebreaker ORDER BY flips this to "Alpha" and fails the test.
    assert rows_a[0]["category"] == "Zeta"
