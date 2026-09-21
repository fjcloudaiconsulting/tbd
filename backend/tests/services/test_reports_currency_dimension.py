"""TBD-507 — the transactions report source gets a currency dimension + filter.

Four of the five report sources already publish currency as a dimension and a
filter (``reports/sources/accounts.py:43,56``, ``recurring.py:54,69``,
``networth.py:65,76``, ``credit_utilization.py:74,90``). ``transactions`` -- the
default source, the one every seeded widget is built on -- publishes neither, so a multi-currency org cannot
separate its own money by hand. TBD-325 PR 2 papered over that with a SCOPE
clause (``reports_query_service.py`` ``org_currency_filter``), which deletes the
non-primary money from the figure rather than partitioning it.

⚠ THE MECHANISM IS PARTITION ON REQUEST, NOT UNCONDITIONAL PARTITION, and the
difference is not a preference. ``credit_utilization.py:305-308`` and
``networth.py:281-283`` can always-group-then-pop because they sort and slice in
PYTHON over the full grouped set, so their "is the whole result one currency?"
predicate sees every row. This compiler applies a SQL ``LIMIT``
(``reports_query_service.py`` step 6), so the same predicate would only ever see
a PAGE: a two-currency org whose first page happens to sort all-EUR would get the
currency key popped and ``warning=None`` -- a partitioned result silently
reported as single-currency. Unconditional partition also regresses the KPI
widget, which sends ``dimensions=[], limit=1``
(``frontend/lib/reports/useReportQuery.ts``) and reads ``rows[0]`` as THE total
(``KpiWidget.tsx``): always-group-by turns one row into N and the slice keeps an
arbitrary one.

⚠ THE SCOPE CLAUSE AND AN EXPLICIT CURRENCY REQUEST ARE CONTRADICTORY. Leave the
scope armed and ``dimensions=[CURRENCY]`` returns exactly ONE row (a currency
breakdown with one bar) while ``filters=[currency in ["USD"]]`` returns ZERO rows
(the scope says EUR, the filter says USD, the conjunction is unsatisfiable).

⚠ ONE function, THREE modes -- ``_currency_mode`` returns ``_SCOPED`` /
``_PARTITIONED`` / ``_MIXED``, and both the WHERE and the ``meta.warning`` read
that one answer, so they cannot disagree. It is deliberately NOT a pair of
booleans: two booleans describe four states, only three are reachable, and the
unreachable fourth ("scoped, yet mixing") is a bug waiting to be constructed.
⚠ An earlier cut used a single BOOLEAN for both questions and shipped exactly
that defect -- see F9, which exists to kill it.

⚠ THE FIXTURES ARE BUILT AT THE ORM LAYER, DELIBERATELY. The ticket's DoD asks
for an API-level multi-currency org; that is not constructible.
``assert_org_currency_allows`` refuses the second currency unconditionally
(``routers/accounts.py``) and the first account is what sets
``primary_currency``, so no public-API sequence produces one -- making it
possible means weakening the door TBD-325 PR 1 exists to hold shut. The
precedent is ``test_currency_scope_aggregates.py``'s ``_bones``/``_account``;
the door itself is fenced separately in
``tests/routers/test_account_currency_door.py``.
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
from app.services import currency_service, reports_query_service

P_START = datetime.date(2026, 6, 1)
P_END = datetime.date(2026, 6, 30)
TXN_DATE = datetime.date(2026, 6, 5)

# ⚠ TWO EUR accounts, on purpose -- but NOT for the reason it is tempting to
# write down. A missing ``Account`` join is caught either way (with one account
# per currency the cross join is 2 txns x 2 accounts = 4 rows and the EUR
# partition reads 5100, not 100), so F2 is not vacuous without the second
# account. What the second EUR account actually buys:
#   * ``EUR_TOTAL`` becomes a TWO-ROW sum, which kills "group by
#     ``Account.name``, label the column ``currency``" -- a mutant that a
#     one-account-per-currency fixture cannot tell from the correct answer;
#   * F5 gets two same-currency rows to fold, so case-folding has something to
#     actually fold.
# Recorded at length because a confident wrong derivation here is how the next
# person mis-sizes a fixture somewhere else.
EUR_MAIN = Decimal("100")
EUR_SAVINGS = Decimal("30")
EUR_TOTAL = EUR_MAIN + EUR_SAVINGS  # 130
USD_AMOUNT = Decimal("5000")
# Only in ``legacy_case``, on an account stored as ``"EUR "`` (trailing space).
EUR_PADDED = Decimal("7")
LEGACY_TOTAL = EUR_MAIN + EUR_SAVINGS + EUR_PADDED  # 137


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine_and_factory() -> AsyncIterator[tuple]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Scoped to THIS engine, not the global ``Engine`` class: ten tests each
    # registering a class-level listener leaves ten of them firing on every
    # engine created later in the process.
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


async def _bones(db, *, primary_currency: str | None) -> dict:
    org = Organization(
        name="Partition Org", billing_cycle_day=1, primary_currency=primary_currency
    )
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    food = Category(org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE)
    db.add(food)
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    return {"org": org, "at": at, "food": food}


async def _account(db, bones, name: str, currency: str) -> Account:
    acct = Account(
        org_id=bones["org"].id,
        account_type_id=bones["at"].id,
        name=name,
        balance=Decimal("0"),
        currency=currency,
    )
    db.add(acct)
    await db.flush()
    return acct


async def _expense(db, bones, acct, amount: Decimal) -> None:
    db.add(
        Transaction(
            org_id=bones["org"].id,
            account_id=acct.id,
            category_id=bones["food"].id,
            description="Groceries",
            amount=amount,
            type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED,
            date=TXN_DATE,
            settled_date=TXN_DATE,
        )
    )
    await db.flush()


@pytest_asyncio.fixture
async def two_currency(db) -> dict:
    """EUR org holding TWO EUR accounts and one USD account.

    The second EUR account is what makes the missing-join mutant visible; see
    the module constants.
    """
    bones = await _bones(db, primary_currency="EUR")
    main = await _account(db, bones, "Main (EUR)", "EUR")
    savings = await _account(db, bones, "Savings (EUR)", "EUR")
    usd = await _account(db, bones, "Dollar (USD)", "USD")
    await _expense(db, bones, main, EUR_MAIN)
    await _expense(db, bones, savings, EUR_SAVINGS)
    await _expense(db, bones, usd, USD_AMOUNT)
    await db.commit()
    return {**bones, "main": main, "savings": savings, "usd": usd}


@pytest_asyncio.fixture
async def legacy_case(db) -> dict:
    """A single-currency org whose two accounts disagree on CASE.

    ``accounts.currency`` was free text before TBD-325 PR 1 and ``AccountUpdate``
    has no currency field, so a stored ``'eur'`` is unrepairable through the
    product. ``primary_currency`` normalises both sides
    (``currency_service.resolve_currency_scope``), so this org is correctly NOT
    scoped -- which is exactly what leaves the dimension and the filter exposed.
    """
    bones = await _bones(db, primary_currency="EUR")
    upper = await _account(db, bones, "Upper (EUR)", "EUR")
    lower = await _account(db, bones, "Legacy (eur)", "eur")
    # ⚠ The PADDED row fences the ``TRIM`` half, and it is the half that matters
    # on PRODUCTION. ``utf8mb4_0900_ai_ci`` is NO PAD, so on MySQL ``'EUR '``
    # really does form its own GROUP BY partition and really does drop out of a
    # filter -- whereas the case half (``'eur'``) is folded by that same
    # collation and is a SQLite-only defect. Without this row, deleting
    # ``func.trim`` from ``_currency_key()`` passes every test here.
    padded = await _account(db, bones, "Padded (EUR )", "EUR ")
    await _expense(db, bones, upper, EUR_MAIN)
    await _expense(db, bones, lower, EUR_SAVINGS)
    await _expense(db, bones, padded, EUR_PADDED)
    await db.commit()
    return {**bones, "upper": upper, "lower": lower, "padded": padded}


def _q(dimensions=None, filters=None, limit: int = 100) -> ReportsQuery:
    return ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=dimensions or [],
        filters=filters or [],
        limit=limit,
    )


# ── F1: the dimension partitions instead of scoping ─────────────────────────


async def test_f1_currency_dimension_partitions_a_two_currency_org(db, two_currency):
    """F1. Kills "the scope WHERE stays armed when CURRENCY is requested".

    Under that mutant the USD rows are filtered out before the GROUP BY, so the
    query returns ONE row -- a currency breakdown with a single bar, which is
    worse than no breakdown because it looks authoritative.
    """
    rows, meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.CATEGORY, Dimension.CURRENCY]),
        org_id=two_currency["org"].id,
    )
    by_pair = {(r["category"], r["currency"]): Decimal(str(r["value"])) for r in rows}

    assert by_pair == {
        ("Food", "EUR"): EUR_TOTAL,
        ("Food", "USD"): USD_AMOUNT,
    }, f"expected both currencies partitioned; got {by_pair}"
    # ⚠ Assert the PAIR, never a total: a total-only assertion is satisfied by
    # offsetting rows and by the naive cross-currency sum alike.
    assert meta["warning"] is None


# ── F2: the dimension needs its JOIN ────────────────────────────────────────


async def test_f2_currency_dimension_does_not_cross_join(db, two_currency):
    """F2. Kills "_DIM_KEYS + _dimension_expr wired, JOIN block not widened".

    Referencing ``Account.currency`` under ``select_from(Transaction)`` with no
    join renders ``FROM transactions, accounts`` -- a silent CROSS JOIN that
    multiplies every row by the account count. It does not raise; it inflates.

    ⚠ Do NOT write "this fence needs two same-currency accounts or it is
    vacuous" -- an earlier draft of this docstring did, and it is false. With
    one account per currency the cross join is 2 txns x 2 accounts = 4 rows and
    the EUR partition reads 5100, so the mutant is caught either way. The real
    reason the fixture holds a second EUR account is at the module constants.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.CURRENCY]),
        org_id=two_currency["org"].id,
    )
    by_ccy = {r["currency"]: Decimal(str(r["value"])) for r in rows}

    assert by_ccy["EUR"] == EUR_TOTAL, (
        f"EUR partition is {by_ccy['EUR']}; the 3-account cross join would give "
        f"{(EUR_TOTAL + USD_AMOUNT) * 2}"
    )
    assert by_ccy["USD"] == USD_AMOUNT
    assert len(rows) == 2


# ── F3: the filter returns rows, not the empty set ──────────────────────────


async def test_f3_currency_filter_returns_that_currency_not_zero_rows(db, two_currency):
    """F3. Kills three mutants at once.

    * no ``_apply_currency_filter`` at all -> ``KeyError`` in ``_FILTER_COLUMN``
      -> 500;
    * the filter wired but the scope NOT stood down -> ``EUR AND USD`` is
      unsatisfiable -> ZERO rows, an empty chart with no explanation;
    * the filter dispatched but ignored -> the EUR money comes back instead.
    """
    rows, meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["USD"])],
        ),
        org_id=two_currency["org"].id,
    )

    assert rows, "currency filter returned no rows: the scope clause is still armed"
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": USD_AMOUNT
    }
    assert meta["warning"] is None


async def test_f3b_currency_filter_eq_matches_the_same_rows(db, two_currency):
    """F3b. ``eq`` and ``in`` are both published by the catalog; both must work."""
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="USD")],
        ),
        org_id=two_currency["org"].id,
    )
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": USD_AMOUNT
    }


# ── F4 / F5: legacy case folding ────────────────────────────────────────────


async def test_f4_currency_filter_matches_a_legacy_lowercase_row(db, legacy_case):
    """F4. Kills the bare ``Account.currency`` on the FILTER side.

    ⚠ RED ON SQLITE, VACUOUS ON MYSQL -- and SQLite is where CI runs (every
    shard but ``Migration Checks``). MySQL's ``utf8mb4_0900_ai_ci`` folds case in
    a comparison, so the bare-column mutant passes there; SQLite does not fold,
    so the stored ``'eur'`` row silently drops out. Do not cite this test as
    MySQL coverage.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"])],
        ),
        org_id=legacy_case["org"].id,
    )
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": LEGACY_TOTAL
    }, "a legacy 'eur' / 'EUR ' account's money went missing"


async def test_f5_currency_dimension_folds_legacy_case_into_one_partition(
    db, legacy_case
):
    """F5. Kills the bare ``Account.currency`` on the GROUP BY side.

    ⚠ NOT the same as F4's asymmetry, despite looking like it. On SQLite the
    bare-column mutant yields two partitions, ``'eur'`` and ``'EUR'``, and this
    goes red. On MySQL ``ai_ci`` folds them into ONE group, so the row count
    holds, but the emitted LABEL is whichever row the optimizer picked -- so on
    MySQL this test is not vacuous, it is FLAKY against the mutant, which is
    worse than useless because an intermittent red reads as a data problem.
    Against CORRECT code it is deterministic on both engines.

    The ``'EUR '`` account additionally fences the ``TRIM`` half, which is a
    real MySQL defect (NO PAD collation), not a SQLite-only one.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.CURRENCY]),
        org_id=legacy_case["org"].id,
    )
    assert [r["currency"] for r in rows] == ["EUR"], (
        f"expected one canonical partition; got {[r['currency'] for r in rows]}"
    )
    assert Decimal(str(rows[0]["value"])) == LEGACY_TOTAL


# ── F6: the notice follows the same predicate as the clause ─────────────────


async def test_f6_warning_is_none_when_the_caller_opted_into_currency(
    db, two_currency
):
    """F6. Kills "the predicate gates the WHERE but not the warning".

    Under that mutant an opted-in query -- which excluded nothing, because every
    currency's rows are in the payload -- still carries the exclusion sentence.
    That sentence would be a lie, and it is the sentence a client renders
    verbatim.

    ⚠ The shape here is deliberately the COMBINED one (dimension AND filter),
    not a loop over the two separately: F1 already asserts ``warning is None``
    for the dimension alone and F3 for the filter alone, so looping would have
    inflated the fence count with two assertions that already exist elsewhere --
    and a ``for`` loop aborts on the first failure, so the second shape would
    have been masked anyway. Dimension-plus-filter is the one combination
    neither F1 nor F3 reaches.
    """
    _rows, meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CURRENCY, Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["USD"])],
        ),
        org_id=two_currency["org"].id,
    )
    assert meta["warning"] is None, (
        f"a query carrying BOTH a currency dimension and a currency filter "
        f"still claims an exclusion: {meta['warning']!r}"
    )


# ── F7: the catalog publishes both, so the source accepts them ──────────────


async def test_f7_transactions_catalog_publishes_currency():
    """F7. Kills "wire the compiler, forget the catalog".

    ``validate_against_catalog`` is what turns a compiler capability into a
    usable one: without the catalog rows the AST is rejected at the source before
    it reaches the compiler, and the frontend never offers the dimension.
    """
    src = registry.get_source("transactions")
    assert "currency" in {d.key for d in src.dimensions()}
    assert "currency" in {f.field for f in src.filters()}

    src.validate(
        _q(
            dimensions=[Dimension.CURRENCY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR"])],
        )
    )


# ── F8: ACCOUNT + CURRENCY together — the hazard the join comment names ─────


async def test_f8_account_and_currency_dimensions_do_not_double_join(
    db, two_currency
):
    """F8. Kills "two separate ``if`` blocks, each joining ``Account``".

    ``MAX_DIMENSIONS`` is 2 and the transactions catalog now publishes both, so
    ``[account, currency]`` is an ordinary thing to pick in the widget editor.
    The ``or`` in the JOIN block exists precisely for it -- joining the same
    table twice raises at compile time.

    ⚠ Every other test in this file requests at most ONE of the two, so the
    refactor into two ``if`` blocks leaves all of them green and 500s here.
    Both reviewers found this gap independently; it was the single named risk
    of the change and it was unfenced.
    """
    rows, meta = await reports_query_service.execute_query(
        db,
        _q(dimensions=[Dimension.ACCOUNT, Dimension.CURRENCY]),
        org_id=two_currency["org"].id,
    )
    assert {(r["account"], r["currency"]): Decimal(str(r["value"])) for r in rows} == {
        ("Main (EUR)", "EUR"): EUR_MAIN,
        ("Savings (EUR)", "EUR"): EUR_SAVINGS,
        ("Dollar (USD)", "USD"): USD_AMOUNT,
    }
    assert meta["warning"] is None


async def test_f8b_currency_filter_alongside_an_account_joining_dimension(
    db, two_currency
):
    """F8b. The shape where ``accounts`` is in the outer FROM *and* the subquery.

    ``_apply_currency_filter`` emits a subquery selecting from ``accounts``;
    grouping by ACCOUNT puts ``accounts`` in the outer FROM too. That is where
    SQLAlchemy decides auto-correlation, and a wrong answer there is silent --
    the ``IN`` degenerates into a per-row test against the joined row instead of
    an independent set.

    Every other executing test pairs the filter with ``[CATEGORY]`` only, where
    ``accounts`` is absent from the outer statement.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.ACCOUNT],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["USD"])],
        ),
        org_id=two_currency["org"].id,
    )
    assert {r["account"]: Decimal(str(r["value"])) for r in rows} == {
        "Dollar (USD)": USD_AMOUNT
    }, "the currency subquery did not behave as an independent set"


# ── F9: a filter naming SEVERAL currencies must not sum them in silence ─────


async def test_f9_multi_currency_filter_says_the_figure_mixes(db, two_currency):
    """F9. Kills "one predicate gates both the WHERE and the notice".

    THE DEFECT THIS FILE ORIGINALLY SHIPPED. ``currency in ["EUR","USD"]`` with
    no currency dimension stands the scope down -- correctly, because scope EUR
    AND filter USD is unsatisfiable -- and under the one-predicate version it
    ALSO suppressed the notice. Result: 130 EUR + 5000 USD returned as an
    unlabelled ``5130`` with ``warning: null``, which is the exact figure the
    whole TBD-325 programme exists to prevent, reached through a new door.

    The figure itself stays available on purpose: refusing a published catalog
    op would make the catalog lie, and on a single-currency org (100% of today's
    corpus) the same request is perfectly well defined. What must never happen
    is returning it in silence.
    """
    rows, meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[
                Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["EUR", "USD"])
            ],
        ),
        org_id=two_currency["org"].id,
    )
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": EUR_TOTAL + USD_AMOUNT
    }
    assert meta["warning"] is not None, (
        "a figure that adds EUR to USD came back with no notice at all"
    )
    # ⚠ Not the EXCLUSION sentence: nothing was excluded from this query. The
    # two facts are different and must not share a string.
    assert "excluded" not in meta["warning"]


async def test_f9b_single_currency_filter_stays_silent(db, two_currency):
    """F9b. The other side of F9: kills "warn whenever a currency filter exists".

    Over-warning is the failure mode a notice-on-everything design produces, and
    this repo has an explicit fence elsewhere against a sankey notice that fires
    when nothing was excluded. One selected currency cannot mix.
    """
    _rows, meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="USD")],
        ),
        org_id=two_currency["org"].id,
    )
    assert meta["warning"] is None


# ── G3: org isolation on the new path (a GUARD — see the docstring) ────────


async def test_g3_currency_filter_does_not_reach_another_org(db, two_currency):
    """GUARD, and deliberately NOT labelled a fence. Read why before reusing it.

    It was written to kill "drop ``Account.org_id == org_id`` from
    ``_apply_currency_filter``'s subquery", because every other fixture here
    builds exactly one org and that argument is otherwise never varied.

    ⚠ IT DOES NOT KILL THAT MUTANT, AND NO TEST CAN. Measured, not assumed:
    deleting that clause and re-running this file gives 16 passed. The reason is
    structural -- the outer ``Transaction.org_id == org_id`` restricts the query
    to this org's transactions, and ``transactions.account_id`` is NOT NULL with
    an FK, so every candidate row already belongs to one of this org's accounts.
    Widening the subquery to every org's USD accounts cannot add a row, because
    the rows were already bounded. The clause is DEFENCE IN DEPTH on an org
    boundary, and redundant-by-construction code is unfenceable by definition.

    Keep the clause (an org boundary is not where redundancy gets trimmed), and
    keep this test: it proves cross-org isolation genuinely holds on the new
    code path. Just do not count it as coverage of the clause itself, and do not
    "fix" it by making it look like one.
    """
    other = await _bones(db, primary_currency="USD")
    other_usd = await _account(db, other, "Other org USD", "USD")
    await _expense(db, other, other_usd, Decimal("999"))
    await db.commit()

    rows, _meta = await reports_query_service.execute_query(
        db,
        _q(
            dimensions=[Dimension.CATEGORY],
            filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.IN, value=["USD"])],
        ),
        org_id=two_currency["org"].id,
    )
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": USD_AMOUNT
    }, "another org's USD money reached this org's report"


# ── F11: through the path the app actually takes ───────────────────────────


async def test_f11_partition_works_through_the_source_not_just_the_compiler(
    db, two_currency
):
    """F11. Kills "the compiler is right but the source never lets it through".

    Every other executing test calls ``execute_query`` directly. The app's path
    is router -> ``registry.get_source("transactions").validate()`` ->
    ``.build_rows()`` -> ``execute_query``, and a fence that skips the first two
    steps cannot see a catalog that rejects the very query the compiler handles.
    """
    src = registry.get_source("transactions")
    query = _q(dimensions=[Dimension.CATEGORY, Dimension.CURRENCY])
    src.validate(query)
    rows, meta = await src.build_rows(db, two_currency["org"].id, query)

    assert {(r["category"], r["currency"]): Decimal(str(r["value"])) for r in rows} == {
        ("Food", "EUR"): EUR_TOTAL,
        ("Food", "USD"): USD_AMOUNT,
    }
    assert meta["warning"] is None


# ── G1 / G2: OVER-REACH CONTROLS — labelled, NOT counted as coverage ────────


async def test_g1_default_query_is_still_scoped_and_still_warns(db, two_currency):
    """OVER-REACH GUARD. Kills "suppress the scope unconditionally".

    A query that names no currency must behave exactly as it does today: scoped
    to the org's primary currency, with the exclusion sentence attached.

    ⚠ READ THE SCOPE OF THE CLAIM PRECISELY, because the first draft of this
    docstring got it backwards. Against the ``two_currency`` FIXTURE this is a
    genuine fence: make ``_currency_mode`` never return ``_SCOPED``
    and it goes red (rows become 5130, not 130). What it is NOT is coverage on
    the PRODUCTION corpus -- ``org_currency_filter`` short-circuits to
    ``true()`` for every org that exists there (zero multi-currency orgs,
    measured 2026-09-03), so against real data it would pass even against
    "delete the feature". Both halves are true and neither implies the other.
    Do not delete it as "just a control".
    """
    rows, meta = await reports_query_service.execute_query(
        db, _q(dimensions=[Dimension.CATEGORY]), org_id=two_currency["org"].id
    )
    assert {r["category"]: Decimal(str(r["value"])) for r in rows} == {
        "Food": EUR_TOTAL
    }, "the default path stopped scoping; the naive sum is EUR + USD"
    # ⚠ Compared against the shared helper's OUTPUT, not against a substring of
    # it. ``currency_warning`` is one string with several consumers, so a
    # legitimate re-word would turn a substring assertion red against correct
    # code -- the inverse-defect shape. This pins "the default path emits THE
    # exclusion sentence" without re-typing the sentence.
    assert meta["warning"] == currency_service.currency_warning(
        {
            "currency": "EUR",
            "excluded_currencies": ["USD"],
            "excluded_account_count": 1,
        }
    )
    assert not any("currency" in r for r in rows), (
        "a currency key leaked into a query that did not ask for one"
    )


async def test_g2_zero_dimension_query_is_untouched(db, two_currency):
    """OVER-REACH GUARD. Kills "partition unconditionally".

    This is the KPI shape: ``dimensions=[], limit=1``. The widget reads
    ``rows[0]`` as THE total, so an implementation that always groups by currency
    returns N rows here, the slice keeps an arbitrary one, and the tile renders
    one currency's subtotal as the org total with ``truncated: true``.
    """
    rows, meta = await reports_query_service.execute_query(
        db, _q(limit=1), org_id=two_currency["org"].id
    )
    assert len(rows) == 1
    assert rows[0] == {"value": float(EUR_TOTAL)}
    assert meta["truncated"] is False
