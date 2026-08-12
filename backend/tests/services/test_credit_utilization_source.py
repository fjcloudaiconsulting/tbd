"""CreditUtilizationSource fences (TBD-170).

Spec: specs/2026-08-11-tbd-170-credit-utilization-reports-source.md

⚠ Fence arithmetic here is COMPUTED, not recalled. An earlier spec draft used
cards (−900/1000) and (−100/9000) and asserted "10.0, not 50.0" — but the
unweighted average of those two is 45.56%, not 50%, so the negative assertion
would have been true under BOTH the right and the wrong implementation: a
vacuous guard inside the very fence written to kill the defect. The fixture
below has a 46.56-point gap, leaving no room for a rounding coincidence.
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

SRC = "credit_utilization"


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


async def _org(db):
    org = Organization(name="Org1", billing_cycle_day=1)
    db.add(org)
    await db.flush()
    cc = AccountType(org_id=org.id, name="Credit Card", slug="credit_card", is_system=False)
    loan = AccountType(org_id=org.id, name="Loan", slug="loan", is_system=False)
    db.add_all([cc, loan])
    await db.flush()
    return org, cc, loan


async def _card(db, org, at, name, balance, limit, *, currency="EUR", active=True):
    a = Account(
        org_id=org.id, name=name, account_type_id=at.id,
        balance=Decimal(str(balance)), currency=currency, is_active=active,
        credit_limit=None if limit is None else Decimal(str(limit)),
    )
    db.add(a)
    await db.flush()
    return a


def _q(measure_field=MeasureField.UTILIZATION_PCT, agg=Aggregation.AVG,
       dimensions=None, filters=None, sort=None, limit=100):
    return ReportsQuery(
        dataset=Dataset.CREDIT_UTILIZATION,
        dimensions=dimensions or [],
        measure=Measure(agg=agg, field=measure_field),
        filters=filters or [],
        sort=sort,
        limit=limit,
    )


async def _run(db, org, q):
    src = registry.get_source(Dataset.CREDIT_UTILIZATION.value)
    src.validate(q)
    return await src.build_rows(db, org.id, q)


# ── F-1: ratio of sums, never an unweighted average ────────────────────────


@pytest.mark.asyncio
async def test_grouped_utilization_is_limit_weighted_not_averaged(db_session):
    """F-1. Kills `func.avg(utilization_pct)` — the plausible wrong number.

    Store card €200 at 100%, big card €20,000 at 5%:
        ratio-of-sums  = 1200/20200*100 =  5.9406%   ← correct
        unweighted avg = (100 + 5)/2    = 52.5000%   ← the defect
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Store", -200, 200)
    await _card(db_session, org, cc, "Big", -1000, 20000)
    await db_session.commit()

    rows, _meta = await _run(db_session, org, _q())
    assert len(rows) == 1
    # abs= is mandatory: 1200/20200*100 = 5.940594059..., which clears
    # pytest's default rel=1e-6 by only 5.9e-12 — a Decimal-vs-float
    # division order would flip this RED against correct code.
    assert rows[0]["value"] == pytest.approx(5.9406, abs=1e-3)
    assert rows[0]["value"] != pytest.approx(52.5, abs=1e-3)


# ── F-4: parity with frontend creditUtilization, over the limit>0 set ──────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "balance,limit,expected",
    [
        (-500, 2000, 25.0),    # ordinary
        (-2000, 2000, 100.0),  # exactly at limit — "fully used", not over
        (-2500, 2000, 125.0),  # F-12: over-limit is NOT clamped
        (0, 2000, 0.0),        # zero balance
        (120, 2000, 0.0),      # F-5.5: overpaid clamps to 0, never negative
    ],
)
async def test_per_card_parity_with_credit_ts(db_session, balance, limit, expected):
    """F-4 + F-12. Mirrors frontend/lib/credit.ts creditUtilization over the
    limit>0 population (the limitless case diverges deliberately — see F-5).

    Kills: a backend on the 0–1 scale (renders "0.5%"), an overpaid card going
    negative, and clamping over-limit to 100 — which would hide the one state
    that actually matters.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "C", balance, limit)
    await db_session.commit()
    rows, _ = await _run(db_session, org, _q())
    assert rows[0]["value"] == pytest.approx(expected, abs=1e-6)


# ── F-5: no-limit cards excluded AND disclosed ────────────────────────────


@pytest.mark.asyncio
async def test_limitless_card_excluded_and_counted_in_warning(db_session):
    """F-5. Single-currency fixture on purpose so the warning under test is
    unambiguously the excluded-card notice (F-16 covers composition).

    Kills: including it at 0% — which credit.ts would hand you — dragging every
    group ratio down and reading as "you're doing great"; and equally, dropping
    it in silence.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Good", -500, 2000)
    await _card(db_session, org, cc, "NoLimit", -900, None)
    await db_session.commit()

    rows, meta = await _run(db_session, org, _q(dimensions=[Dimension.ACCOUNT]))
    assert [r["account"] for r in rows] == ["Good"]
    assert rows[0]["value"] == pytest.approx(25.0, abs=1e-6)
    assert "no credit limit set" in (meta.get("warning") or "")
    assert "1 credit card(s) excluded" in (meta.get("warning") or "")


@pytest.mark.asyncio
async def test_loans_and_foreign_orgs_are_not_credit_cards(db_session):
    """F-5.1/5.2. Gate is AccountType.slug, and org-scoping holds."""
    org, cc, loan = await _org(db_session)
    await _card(db_session, org, cc, "Card", -500, 2000)
    await _card(db_session, org, loan, "Loan", -5000, 10000)
    await db_session.commit()
    rows, _ = await _run(db_session, org, _q(dimensions=[Dimension.ACCOUNT]))
    assert [r["account"] for r in rows] == ["Card"]


# ── F-2 / F-16: currency partitioning and composed warnings ───────────────


@pytest.mark.asyncio
async def test_multi_currency_never_merges_and_warns(db_session):
    """F-2. Kills the trap: a percentage LOOKS currency-free, so aggregating
    sums EUR and USD into both numerator and denominator — an implicit 1.00 FX
    — and the "never sum across currencies" reflex never fires because the
    output is a percent.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Eur", -900, 1000, currency="EUR")   # 90%
    await _card(db_session, org, cc, "Usd", -100, 1000, currency="USD")   # 10%
    await db_session.commit()

    rows, meta = await _run(db_session, org, _q())
    assert len(rows) == 2, "currencies must never be merged into one row"
    by_ccy = {r["currency"]: r["value"] for r in rows}
    assert by_ccy["EUR"] == pytest.approx(90.0, abs=1e-6)
    assert by_ccy["USD"] == pytest.approx(10.0, abs=1e-6)
    # The merged-but-wrong answer would be 1000/2000 = 50.0.
    assert all(r["value"] != pytest.approx(50.0, abs=1e-3) for r in rows)
    assert "currencies are never summed" in (meta.get("warning") or "")


@pytest.mark.asyncio
async def test_single_currency_drops_the_partition_key(db_session):
    """The currency key is carried for partitioning only; a single-currency
    org must not be given a stray column it did not ask for."""
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "A", -500, 2000)
    await db_session.commit()
    rows, meta = await _run(db_session, org, _q())
    assert "currency" not in rows[0]
    assert meta.get("warning") is None


@pytest.mark.asyncio
async def test_both_notices_compose_into_one_warning(db_session):
    """F-16. QueryMeta.warning is a single Optional[str]; both notices can
    apply at once.

    Kills: a second assignment silently discarding the first, so the user is
    told about currencies and never about the excluded card.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Eur", -900, 1000, currency="EUR")
    await _card(db_session, org, cc, "Usd", -100, 1000, currency="USD")
    await _card(db_session, org, cc, "NoLimit", -50, None)
    await db_session.commit()

    _rows, meta = await _run(db_session, org, _q())
    warning = meta.get("warning") or ""
    assert "currencies are never summed" in warning
    assert "no credit limit set" in warning


# ── F-14: every measure returns ITS OWN number ────────────────────────────


@pytest.mark.asyncio
async def test_each_measure_field_returns_its_own_value(db_session):
    """F-14. A QueryRow carries exactly ONE `value`.

    Kills: a build_rows that emits the utilization figure for every requested
    measure — a KPI asking for card count rendering "2 cards" as 45.0.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "A", -500, 2000)
    await _card(db_session, org, cc, "B", -400, 2000)
    await db_session.commit()

    async def val(field, agg):
        rows, _ = await _run(db_session, org, _q(measure_field=field, agg=agg))
        return rows[0]["value"]

    assert await val(MeasureField.UTILIZATION_PCT, Aggregation.AVG) == pytest.approx(22.5, abs=1e-6)
    assert await val(MeasureField.OUTSTANDING, Aggregation.SUM) == pytest.approx(900.0, abs=1e-6)
    assert await val(MeasureField.CREDIT_LIMIT, Aggregation.SUM) == pytest.approx(4000.0, abs=1e-6)
    assert await val(MeasureField.ID, Aggregation.COUNT) == pytest.approx(2.0, abs=1e-6)


# ── F-3 / F-15: the agg pin is exhaustive ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,bad_agg",
    [
        (MeasureField.UTILIZATION_PCT, Aggregation.SUM),   # F-3
        (MeasureField.OUTSTANDING, Aggregation.AVG),       # F-15
        (MeasureField.CREDIT_LIMIT, Aggregation.AVG),
    ],
)
async def test_wrong_agg_is_rejected(db_session, field, bad_agg):
    """F-3 + F-15. validate_against_catalog checks the FIELD, never the AGG.

    Kills: gating only utilization_pct. build_rows only ever SUMs, so
    `avg(outstanding)` over two cards at €900/€100 would return €1,000 and
    call it an average.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "A", -500, 2000)
    await db_session.commit()
    src = registry.get_source(SRC)
    with pytest.raises(ValueError):
        src.validate(_q(measure_field=field, agg=bad_agg))


# ── F-13: published filters are actually COMPILED ─────────────────────────


@pytest.mark.asyncio
async def test_currency_filter_is_honoured(db_session):
    """F-13. Publishing a filter is not honouring it — validate_against_catalog
    ACCEPTS a published field without applying it, so an uncompiled filter is
    silently ignored with no error anywhere.
    """
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Eur", -900, 1000, currency="EUR")
    await _card(db_session, org, cc, "Usd", -100, 1000, currency="USD")
    await db_session.commit()

    rows, _ = await _run(db_session, org, _q(
        dimensions=[Dimension.ACCOUNT],
        filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="USD")],
    ))
    assert [r["account"] for r in rows] == ["Usd"]


@pytest.mark.asyncio
async def test_account_active_filter_is_honoured(db_session):
    """F-13. Inactive cards are IN the row set by default (a closed card with a
    balance still consumes utilization), so the filter is the only way to
    exclude them — it must actually work."""
    org, cc, _ = await _org(db_session)
    await _card(db_session, org, cc, "Live", -500, 2000, active=True)
    await _card(db_session, org, cc, "Closed", -900, 2000, active=False)
    await db_session.commit()

    rows, _ = await _run(db_session, org, _q(dimensions=[Dimension.ACCOUNT]))
    assert sorted(r["account"] for r in rows) == ["Closed", "Live"], "inactive included by default"

    rows, _ = await _run(db_session, org, _q(
        dimensions=[Dimension.ACCOUNT],
        filters=[Filter(field=FilterField.ACCOUNT_ACTIVE, op=FilterOp.EQ, value=True)],
    ))
    assert [r["account"] for r in rows] == ["Live"]


@pytest.mark.asyncio
async def test_account_id_filter_is_honoured(db_session):
    """F-13."""
    org, cc, _ = await _org(db_session)
    a = await _card(db_session, org, cc, "A", -500, 2000)
    await _card(db_session, org, cc, "B", -400, 2000)
    await db_session.commit()

    rows, _ = await _run(db_session, org, _q(
        dimensions=[Dimension.ACCOUNT],
        filters=[Filter(field=FilterField.ACCOUNT_ID, op=FilterOp.IN, value=[a.id])],
    ))
    assert [r["account"] for r in rows] == ["A"]


# ── F-10: no time dimension, no date filter ───────────────────────────────


def test_publishes_no_time_dimension_and_no_date_filter():
    """F-10. Kills someone "helpfully" adding `month` by reusing networth's
    reconstruction against TODAY's credit_limit — a chart that silently
    rewrites last January every time the user gets a limit increase.

    There is no credit-limit history anywhere: Account.credit_limit is a
    mutable scalar overwritten in place, with no snapshot table and no audit
    row. Adding a time dimension needs that substrate FIRST.
    """
    src = registry.get_source(SRC)
    assert {d.key for d in src.dimensions()} == {"account", "currency", "account_active"}
    assert "date" not in {f.field for f in src.filters()}
    assert src.filters(), "every source must publish a non-empty filter list"


# ── F-17: sort + slice happen in Python, on the computed value ────────────


@pytest.mark.asyncio
async def test_limit_keeps_the_top_rows_by_utilization(db_session):
    """F-17. utilization_pct exists only AFTER the Python division, so a SQL
    .limit() would keep an arbitrary N rows and the Python sort would then
    order the wrong ones — silently dropping the highest-utilization cards,
    which are the entire point of the report.

    30 cards at distinct utilizations 1%..30%; ask for the top 5.
    """
    org, cc, _ = await _org(db_session)
    for i in range(1, 31):
        await _card(db_session, org, cc, f"Card{i:02d}", -(i * 10), 1000)
    await db_session.commit()

    rows, meta = await _run(db_session, org, _q(
        dimensions=[Dimension.ACCOUNT],
        sort=SortSpec(by=SortBy.VALUE, dir=SortDir.DESC),
        limit=5,
    ))
    assert [r["account"] for r in rows] == [
        "Card30", "Card29", "Card28", "Card27", "Card26",
    ]
    assert rows[0]["value"] == pytest.approx(30.0, abs=1e-6)
    assert meta["truncated"] is True


@pytest.mark.asyncio
async def test_sort_by_dimension_without_a_dimension_is_a_validation_error(db_session):
    """The check must live in validate(), not build_rows: _run_source_query
    wraps only validate() in its try, so raising in build_rows turns user
    input into a 500 instead of a 422."""
    src = registry.get_source(SRC)
    with pytest.raises(ValueError):
        src.validate(_q(sort=SortSpec(by=SortBy.DIMENSION, dir=SortDir.ASC)))
