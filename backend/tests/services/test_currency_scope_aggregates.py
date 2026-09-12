"""Period aggregates answer in ONE currency (TBD-325, PR 2).

Spec: ``specs/2026-09-12-tbd-325-pr2-currency-scope.md``.
Sibling: ``tests/routers/test_account_currency_door.py`` (PR 1), which closed
the *first* of the two account-insert sites. This file fences the second door
(F4) and the thing both doors exist for: the aggregates themselves.

What is actually wrong without this PR
--------------------------------------
``transactions`` has no currency column. Currency lives only on
``accounts.currency``, and not one period aggregate joins ``Account``. So an
org holding a USD account and a EUR account gets On Track, the budget bars, the
spending donut, the forecast projection and the Sankey ribbons all reporting
``EUR + USD`` as a single unlabelled number. There is no FX anywhere in the
system, so the only honest answer is to SCOPE to the org's one currency and
say, in band, what was left out.

⚠⚠ THE VACUITY TRAP IN F3, AND WHY THE FIXTURE LOOKS OVER-SPECIFIED
--------------------------------------------------------------------
``transactions`` stores POSITIVE amounts with a ``type`` discriminator, so
"EUR -100 against USD +5000" is not expressible, and — worse — two figures that
merely DIFFER can still render the SAME verdict. ``OnTrackTile`` bands at
0.95 / 1.05. Against a plan of 500:

    EUR 100 + USD 100  ->  scoped 0.20, naive 0.40   BOTH "ON TRACK"
    EUR 100 + USD 5000 ->  scoped 0.20, naive 10.2   "ON TRACK" vs "OVER BUDGET"

The first fixture passes against an implementation with the predicate deleted.
That is why the USD leg is 5000 and not 100, and why
``test_the_scoped_figure_crosses_a_rendered_verdict_boundary`` exists as its own
assertion rather than a comment: the numbers are load-bearing and the next
person to "tidy" them needs to be stopped by a red test, not by prose.

⚠ WHY THE TWO-CURRENCY FIXTURE IS BUILT BY DIRECT INSERT
---------------------------------------------------------
It is NOT because the state is unreachable. It was reachable through two
ordinary API calls until this PR — that is exactly what F4 fences and what the
2026-09-12 measurement confirmed on real MySQL:

    POST /accounts USD          -> 201
    POST /onboarding/seed-demo   -> 200, two EUR accounts + EUR transactions
    DISTINCT CURRENCIES          -> ['EUR', 'USD']

PR 2 closes that path, so after this PR the fixture can no longer be built
through the API — which is why F4 is the API-level test and F3 is not. The
state remains reachable from a bulk import, a restore, a future third insert
site, and every row that predates the door. ``OnTrackTile``'s guard carries the
same "safe BY MEASUREMENT, not by construction" note for the same reason.

⚠ WHAT IS A FENCE HERE AND WHAT IS NOT
---------------------------------------
Every test below names the wrong implementation it kills. ``G1`` does not, and
says so: it passes against today's unmodified code and is an OVER-REACH
CONTROL, not a fence — same posture as ``test_a_valid_iso_code_is_accepted``
in the PR 1 file.

F7 is written against ``ai_forecast_refine_service``, which is being scoped in
parallel with this file. See its own docstring for what it can and cannot see.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.forecast_plan import ForecastPlanItem, ItemSource
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
from app.schemas.reports_query import Measure, ReportsQuery, SankeyQuery
from app.services import (
    budget_draft_service,
    budget_rebalance_service,
    budget_service,
    currency_service,
    forecast_plan_service,
    forecast_service,
    reports_query_service,
    sankey_service,
    spending_service,
)

# June 2026, driven by an INJECTED clock. Every call below passes ``today=``,
# so nothing here is a wall-clock date bomb.
P_START = datetime.date(2026, 6, 1)
P_END = datetime.date(2026, 6, 30)
TODAY = datetime.date(2026, 6, 30)

# The fixture's money, in one place because the verdict-boundary argument in
# the module docstring depends on the exact values.
PLAN = Decimal("500")
EUR_EXECUTED = Decimal("100")
USD_EXECUTED = Decimal("5000")
EUR_PENDING = Decimal("40")
USD_PENDING = Decimal("4000")
EUR_INCOME = Decimal("700")
USD_INCOME = Decimal("9000")
EUR_RECURRING = Decimal("25")
USD_RECURRING = Decimal("3000")

# ── the PRE-PERIOD money, for the five sites fenced at the bottom of this file
#
# Everything above answers about ONE period. The plan/draft/rebalance surfaces
# average over the THREE MONTHS BEFORE it, and ``populate_from_sources`` reads
# three windows at once, so those sites need history the fixtures above do not
# have. April + May 2026 — the two months inside ``[P_START - 3mo, P_START)``.
HIST_MONTHS = (4, 5)
EUR_FOOD_HIST = Decimal("120")     # per month, on ``food``
USD_FOOD_HIST = Decimal("6000")
EUR_RENT_HIST = Decimal("200")     # per month, on ``rent`` (history ONLY)
USD_RENT_HIST = Decimal("7000")
EUR_TRAVEL_NOW = Decimal("60")     # in-period only, on ``travel``
USD_TRAVEL_NOW = Decimal("6000")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine_and_factory() -> AsyncIterator[tuple]:
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
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db(engine_and_factory) -> AsyncIterator[AsyncSession]:
    _engine, factory = engine_and_factory
    async with factory() as session:
        yield session


@contextmanager
def capture_sql(engine):
    """Every statement the engine actually executes, in order.

    ⚠ This is how F1 and F2 avoid the V1 vacuity trap. ``str(stmt)`` on a
    hand-built statement proves only that the helper short-circuits for the
    arguments the TEST chose; it says nothing about what the service passes,
    and it is structurally blind to the recurring path, which under the wrong
    implementation raises ``MissingGreenlet`` instead of compiling anything.
    """
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _ctx, _many):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _is_aggregate(statement: str) -> bool:
    """A statement that reads money rows, as opposed to the scope resolution.

    The scope resolution reads ``accounts`` BY NECESSITY (it is a LEFT JOIN of
    the org row onto its accounts) — F1 is restated in the spec precisely
    because an earlier draft asserted "no statement anywhere references
    accounts", which is unsatisfiable. What must never reference ``accounts``
    is a statement that sums or selects transactions.
    """
    s = statement.lower()
    return "from transactions" in s or "from recurring_transactions" in s


async def _bones(db, *, primary_currency: str | None) -> dict:
    org = Organization(
        name="Scope Org", billing_cycle_day=1, primary_currency=primary_currency
    )
    db.add(org)
    await db.flush()
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add(at)
    await db.flush()
    food = Category(
        org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE
    )
    pay = Category(
        org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME
    )
    db.add_all([food, pay])
    db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
    await db.flush()
    return {"org": org, "at": at, "food": food, "pay": pay}


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


async def _money(db, bones, acct, *, eur_leg: bool) -> None:
    """One account's worth of June money: settled, pending, income, recurring."""
    org_id = bones["org"].id
    executed = EUR_EXECUTED if eur_leg else USD_EXECUTED
    pending = EUR_PENDING if eur_leg else USD_PENDING
    income = EUR_INCOME if eur_leg else USD_INCOME
    recurring = EUR_RECURRING if eur_leg else USD_RECURRING
    db.add_all([
        Transaction(
            org_id=org_id, account_id=acct.id, category_id=bones["food"].id,
            description="Groceries", amount=executed,
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 6, 5), settled_date=datetime.date(2026, 6, 5),
        ),
        Transaction(
            org_id=org_id, account_id=acct.id, category_id=bones["food"].id,
            description="Not settled yet", amount=pending,
            type=TransactionType.EXPENSE, status=TransactionStatus.PENDING,
            date=datetime.date(2026, 6, 10), settled_date=datetime.date(2026, 6, 10),
        ),
        Transaction(
            org_id=org_id, account_id=acct.id, category_id=bones["pay"].id,
            description="Payday", amount=income,
            type=TransactionType.INCOME, status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 6, 2), settled_date=datetime.date(2026, 6, 2),
        ),
        RecurringTransaction(
            org_id=org_id, account_id=acct.id, category_id=bones["food"].id,
            description="Weekly veg box", amount=recurring,
            type="expense", frequency=Frequency.MONTHLY,
            next_due_date=datetime.date(2026, 6, 20), is_active=True,
        ),
    ])
    await db.flush()


@pytest_asyncio.fixture
async def two_currency(db) -> dict:
    """EUR org (``primary_currency='EUR'``) that also holds ONE USD account.

    The USD account carries 5000 of settled expense against the EUR account's
    100, so the scoped and the naive answer land on OPPOSITE SIDES of the
    rendered verdict band. See the module docstring.
    """
    bones = await _bones(db, primary_currency="EUR")
    eur = await _account(db, bones, "Main (EUR)", "EUR")
    usd = await _account(db, bones, "Dollar (USD)", "USD")
    await _money(db, bones, eur, eur_leg=True)
    await _money(db, bones, usd, eur_leg=False)
    db.add(Budget(
        org_id=bones["org"].id, category_id=bones["food"].id,
        amount=PLAN, period_start=P_START, period_end=P_END,
    ))
    await db.commit()
    return {**bones, "eur": eur, "usd": usd}


@pytest_asyncio.fixture
async def single_currency(db) -> dict:
    """An ORDINARY org: one currency, and a NON-NULL ``primary_currency``.

    ⚠ This shape is the whole reason F1 exists. The first cut of the mechanism
    short-circuited on ``primary_currency is None``, which is true only for
    zero-account and legacy-multi orgs — so every ordinary org in the product
    would have carried a correlated subquery on every aggregate, to select rows
    that were all selected anyway. Measured: 13 statements against a baseline
    of 11, two of them referencing ``accounts``.
    """
    bones = await _bones(db, primary_currency="EUR")
    eur = await _account(db, bones, "Main (EUR)", "EUR")
    await _money(db, bones, eur, eur_leg=True)
    await db.commit()
    return {**bones, "eur": eur}


# ── F1: an ordinary org pays nothing for the scoping ────────────────────────


async def test_f1_no_aggregate_references_accounts_for_a_single_currency_org(
    db, engine_and_factory, single_currency
):
    """F1. Kills a predicate that always joins, AND the NULL-keyed short-circuit.

    The short-circuit must be on ``excluded_account_count == 0``, not on
    ``currency is None``: an ordinary single-currency org has a NON-NULL
    ``primary_currency``, so keying on NULL-ness scopes 100% of orgs to change
    no result.

    ⚠ Asserted on the AGGREGATE statements only. The scope resolution reads
    ``accounts`` by necessity and is excluded by ``_is_aggregate`` — asserting
    "no statement anywhere" is unsatisfiable, which is how the first draft of
    this fence was written and why the spec restates it.
    """
    engine, _ = engine_and_factory
    org_id = single_currency["org"].id
    with capture_sql(engine) as seen:
        await forecast_service.compute_forecast(
            db, org_id, period_start=P_START, today=TODAY
        )

    aggregates = [s for s in seen if _is_aggregate(s)]
    assert aggregates, "captured no aggregate statements — the fence is vacuous"
    offenders = [s for s in aggregates if "accounts" in s.lower()]
    assert not offenders, (
        f"{len(offenders)} of {len(aggregates)} aggregate statements reference "
        f"`accounts` for an org with nothing to exclude. First:\n"
        f"{offenders[0][:600]}"
    )


# ── F2: the in-band scope costs exactly one query, not one per site ─────────


async def test_f2_compute_forecast_costs_exactly_one_extra_query(
    db, engine_and_factory, single_currency, monkeypatch
):
    """F2. Kills per-aggregate scope resolution (+N instead of +1).

    ⚠ "Unchanged" is UNACHIEVABLE and the spec says so: an in-band
    ``currency_scope`` must read the account-currency distribution at least
    once. ``baseline + 1`` is the floor.

    The baseline is measured by stubbing ``resolve_currency_scope`` to return
    the same dict WITHOUT issuing SQL — so the compared runs differ in exactly
    one thing, the scope query. There is no circularity: an implementation that
    resolved the scope per aggregate would call the stub N times (adding
    nothing to the baseline) and the real function N times, so the delta would
    be N, not 1.

    ⚠ This also covers what F1 structurally cannot see. The recurring path
    under the wrong implementation (touching ``r.account.currency`` in the
    occurrence loop) raises ``MissingGreenlet`` on an AsyncSession — it CRASHES
    rather than compiling a statement, so a fence that only inspects compiled
    SQL is blind to it. Measured 2026-09-12: the loop variant raises
    ``StatementError: greenlet_spawn has not been called``.
    """
    engine, _ = engine_and_factory
    org_id = single_currency["org"].id
    real = currency_service.resolve_currency_scope
    scope = await real(db, org_id=org_id)

    async def _stub(_db, *, org_id):  # noqa: ARG001 - signature parity
        return scope

    monkeypatch.setattr(currency_service, "resolve_currency_scope", _stub)
    with capture_sql(engine) as baseline_stmts:
        await forecast_service.compute_forecast(
            db, org_id, period_start=P_START, today=TODAY
        )
    monkeypatch.undo()

    with capture_sql(engine) as real_stmts:
        await forecast_service.compute_forecast(
            db, org_id, period_start=P_START, today=TODAY
        )

    assert len(real_stmts) == len(baseline_stmts) + 1, (
        f"compute_forecast issued {len(real_stmts)} statements against a "
        f"baseline of {len(baseline_stmts)}; the in-band scope must cost "
        f"exactly one. Extra:\n"
        + "\n".join(s[:200] for s in real_stmts if s not in baseline_stmts)
    )


# ── F3: a two-currency org gets the scoped figure, not the naive sum ────────


async def test_f3_compute_forecast_scopes_every_bucket_and_the_categories(
    db, two_currency
):
    """F3. Kills the predicate removed from any bucket in ``compute_forecast``.

    Four buckets and the per-category breakdown, asserted independently,
    because they are five separate ``.where()`` clauses and a predicate dropped
    from one of them is invisible to a totals-only assertion.
    """
    fc = await forecast_service.compute_forecast(
        db, two_currency["org"].id, period_start=P_START, today=TODAY
    )

    assert Decimal(fc["executed_expense"]) == EUR_EXECUTED, (
        f"executed_expense is {fc['executed_expense']}; the naive "
        f"cross-currency sum is {EUR_EXECUTED + USD_EXECUTED}"
    )
    assert Decimal(fc["pending_expense"]) == EUR_PENDING
    assert Decimal(fc["executed_income"]) == EUR_INCOME
    assert Decimal(fc["recurring_expense"]) == EUR_RECURRING, (
        "the recurring projection is scoped on the SELECT, not in the loop — "
        "see forecast_service's ⚠ note; the loop variant raises MissingGreenlet"
    )

    food = next(
        c for c in fc["categories"] if c["category_id"] == two_currency["food"].id
    )
    assert Decimal(food["executed"]) == EUR_EXECUTED
    assert Decimal(food["pending"]) == EUR_PENDING
    assert Decimal(food["recurring"]) == EUR_RECURRING
    assert Decimal(food["forecast"]) == EUR_EXECUTED + EUR_PENDING + EUR_RECURRING

    assert fc["currency_scope"] == {
        "currency": "EUR",
        "excluded_currencies": ["USD"],
        "excluded_account_count": 1,
    }


def _verdict(pct: float) -> str:
    """``OnTrackTile.computeVerdict``, transcribed. Bands at 0.95 / 1.05."""
    if pct <= 0.95:
        return "ON TRACK"
    if pct <= 1.05:
        return "WATCH"
    return "OVER BUDGET"


async def test_f3_the_scoped_figure_crosses_a_rendered_verdict_boundary(
    db, two_currency
):
    """⚠⚠ THE ANTI-VACUITY ASSERTION FOR THE WHOLE F3 BLOCK.

    Two figures that merely DIFFER can still render the same verdict, and a
    fixture that produces one passes against an implementation with the
    predicate deleted. This asserts the fixture is strong enough to be worth
    anything: scoped and naive must land on opposite sides of a band the user
    can actually see.

    Delete the predicate and ``OnTrackTile`` renders a green "ON TRACK" over
    spending the scope deleted — a WRONG VERDICT in a money product, not a
    missing one.
    """
    fc = await forecast_service.compute_forecast(
        db, two_currency["org"].id, period_start=P_START, today=TODAY
    )
    scoped = Decimal(fc["executed_expense"])
    naive = EUR_EXECUTED + USD_EXECUTED

    assert _verdict(float(scoped / PLAN)) == "ON TRACK"
    assert _verdict(float(naive / PLAN)) == "OVER BUDGET"
    assert _verdict(float(scoped / PLAN)) != _verdict(float(naive / PLAN))


async def test_f3_budget_spent_is_scoped(db, two_currency):
    """F3. Kills the predicate removed from ``budget_service._compute_spent``.

    Driven through ``list_budgets`` rather than the private helper, because
    ``_compute_spent`` takes ``currency_scope`` as a PARAMETER defaulting to
    ``None``: calling it directly with no scope would assert nothing about
    whether any real entry point ever resolves one.
    """
    rows = await budget_service.list_budgets(
        db, two_currency["org"].id, P_START, today=TODAY
    )
    assert len(rows) == 1
    assert Decimal(rows[0].spent) == EUR_EXECUTED, (
        f"budget spent is {rows[0].spent}; the naive cross-currency sum is "
        f"{EUR_EXECUTED + USD_EXECUTED}"
    )


async def test_f3_executed_expense_by_category_is_scoped(db, two_currency):
    """F3. Kills the predicate removed from ``spending_service``.

    Driven through ``compute_spending_by_category`` (the dashboard donut's
    entry point) for the same reason as the budget fence above — the shared
    rollup's ``currency_scope`` defaults to ``None``.

    ⚠ NOT "the forecast total equals the donut centre" (spec V3): both
    surfaces call the SAME function object, so they agree whether or not the
    predicate is applied. That test is vacuous today and would stay vacuous.
    """
    out = await spending_service.compute_spending_by_category(
        db, two_currency["org"].id, P_START, today=TODAY
    )
    assert Decimal(out["executed_expense"]) == EUR_EXECUTED
    food = next(
        c for c in out["categories"] if c["category_id"] == two_currency["food"].id
    )
    assert Decimal(food["executed"]) == EUR_EXECUTED


async def test_f3_sankey_ribbons_are_scoped(db, two_currency):
    """F3. Kills the predicate removed from either Sankey aggregation.

    Income and expense are separate statements, so both are asserted: a
    predicate dropped from the income side alone leaves the expense ribbons
    correct and silently inflates the savings remainder.
    """
    res = await sankey_service.build_sankey(
        db, org_id=two_currency["org"].id, query=SankeyQuery(filters=[])
    )
    by_target = {(l.source, l.target): Decimal(str(l.value)) for l in res.links}
    income = sum(v for (_s, t), v in by_target.items() if t == sankey_service.HUB_INCOME)
    expense = sum(
        v for (s, t), v in by_target.items()
        if s == sankey_service.HUB_INCOME and t != sankey_service.HUB_SAVINGS
    )
    # ⚠ The Sankey has NO status filter -- it is a cash-flow picture, so it
    # draws settled AND pending. Both EUR legs, therefore, and both USD legs in
    # the naive figure. Asserting only the settled leg here would have been a
    # test written against the forecast's semantics, not the Sankey's.
    assert income == EUR_INCOME, (
        f"income ribbons total {income}, naive is {EUR_INCOME + USD_INCOME}"
    )
    assert expense == EUR_EXECUTED + EUR_PENDING, (
        f"expense ribbons total {expense}, naive is "
        f"{EUR_EXECUTED + EUR_PENDING + USD_EXECUTED + USD_PENDING}"
    )


# ── F8: the Sankey says money was left out ──────────────────────────────────


async def test_f8_sankey_warns_when_money_was_excluded(db, two_currency):
    """F8. Kills the warning dropped.

    The ribbons are correct-but-incomplete. Without the notice the user sees a
    smaller chart and no reason for it, which is the same failure mode as an
    unlabelled cross-currency sum, only quieter.
    """
    res = await sankey_service.build_sankey(
        db, org_id=two_currency["org"].id, query=SankeyQuery(filters=[])
    )
    assert res.meta.warning is not None
    assert "EUR" in res.meta.warning and "USD" in res.meta.warning, res.meta.warning


async def test_f8_sankey_is_silent_when_nothing_was_excluded(db, single_currency):
    """F8, the other direction. Kills a warning emitted unconditionally.

    A notice on 100% of orgs is a notice nobody reads, and it would announce an
    exclusion that did not happen.
    """
    res = await sankey_service.build_sankey(
        db, org_id=single_currency["org"].id, query=SankeyQuery(filters=[])
    )
    assert res.meta.warning is None, res.meta.warning


# ── F5 / F6: the column, its one writer, and its NULLs ──────────────────────


async def test_f5_the_first_account_writes_the_orgs_primary_currency(db):
    """F5. Kills the writer removed, and a hardcoded value.

    ⚠ The org takes JPY, not EUR. With EUR the assertion is satisfied by a
    literal ``primary_currency="EUR"`` anywhere in the write — the same mutant
    the PR 1 file defends against by seeding JPY. JPY appears in no default in
    this codepath.

    ⚠ WHAT THIS CANNOT SEE, stated rather than implied: moving the write BELOW
    the mismatch check is unobservable, because every accepted account after
    the first carries the currency already stored, so the value written is the
    same. Placement inside the ``existing is None`` branch is enforced by
    reading the function, not by this test.
    """
    bones = await _bones(db, primary_currency=None)
    await db.commit()
    org_id = bones["org"].id

    assert (await db.get(Organization, org_id)).primary_currency is None

    await currency_service.assert_org_currency_allows(db, org_id=org_id, currency="jpy")
    await _account(db, bones, "First", "JPY")
    await db.commit()
    db.expire_all()

    org = await db.get(Organization, org_id)
    assert org.primary_currency == "JPY", (
        f"primary_currency is {org.primary_currency!r} after the first account "
        f"was created in JPY"
    )
    stored = (
        await db.execute(select(Account.currency).where(Account.org_id == org_id))
    ).scalars().all()
    assert org.primary_currency == stored[0], (
        "the cache disagrees with the account it caches"
    )


async def test_f6_the_column_is_nullable_with_no_server_default():
    """F6. Kills ``server_default="EUR"``.

    A default manufactures the "org says EUR, accounts say GBP" divergence for
    every existing row on migration day — and silently, because the predicate
    would then scope real orgs to a currency they never chose.
    """
    col = Organization.__table__.c.primary_currency
    assert col.nullable is True
    assert col.server_default is None, (
        f"primary_currency carries server_default={col.server_default!r}"
    )
    assert col.default is None, f"primary_currency carries default={col.default!r}"


async def test_f6_a_zero_account_org_resolves_to_null_and_scopes_nothing(db):
    """F6. The zero-account org, end to end through the resolver.

    NULL must mean "do not scope", not "scope to nothing" — the latter would
    return zero for every aggregate on a brand-new org.
    """
    bones = await _bones(db, primary_currency=None)
    await db.commit()
    scope = await currency_service.resolve_currency_scope(db, org_id=bones["org"].id)
    assert scope == {
        "currency": None,
        "excluded_currencies": [],
        "excluded_account_count": 0,
    }


async def test_f6_a_legacy_multi_currency_org_resolves_to_null_and_scopes_nothing(db):
    """F6. The legacy multi-currency org (the migration backfills it to NULL).

    ⚠ This is the shape that must stay byte-identical to pre-PR behaviour. An
    org holding EUR and USD from before the door existed has no defensible
    "primary" currency, so scoping it to one would silently DELETE half its
    money from every aggregate — strictly worse than the unlabelled sum it has
    today. The honest answer is to leave it unscoped until someone decides.
    """
    bones = await _bones(db, primary_currency=None)
    eur = await _account(db, bones, "Legacy EUR", "EUR")
    await _account(db, bones, "Legacy USD", "USD")
    await _money(db, bones, eur, eur_leg=True)
    await db.commit()

    scope = await currency_service.resolve_currency_scope(db, org_id=bones["org"].id)
    assert scope["currency"] is None
    assert scope["excluded_account_count"] == 0

    fc = await forecast_service.compute_forecast(
        db, bones["org"].id, period_start=P_START, today=TODAY
    )
    assert Decimal(fc["executed_expense"]) == EUR_EXECUTED


# ── G1: NOT A FENCE ─────────────────────────────────────────────────────────


async def test_g1_single_currency_output_is_unchanged(db, single_currency):
    """⚠ OVER-REACH CONTROL, not a fence. Passes against today's unmodified code.

    Every assertion above is satisfied by "scope everything to nothing" or by
    "return zero whenever a second currency exists anywhere". This is the test
    that says the 99% of orgs who have one currency still get their money back,
    unchanged, with an empty scope declaration.

    It cannot fail against a broken scoping implementation UNLESS that
    implementation also breaks the ordinary path — which is exactly its job,
    and exactly why it is labelled rather than counted as coverage.
    """
    fc = await forecast_service.compute_forecast(
        db, single_currency["org"].id, period_start=P_START, today=TODAY
    )
    assert Decimal(fc["executed_expense"]) == EUR_EXECUTED
    assert Decimal(fc["pending_expense"]) == EUR_PENDING
    assert Decimal(fc["executed_income"]) == EUR_INCOME
    assert Decimal(fc["recurring_expense"]) == EUR_RECURRING
    assert fc["currency_scope"] == {
        "currency": "EUR",
        "excluded_currencies": [],
        "excluded_account_count": 0,
    }


# ── F6 (second half): the MIGRATION's backfill, run for real ────────────────
#
# The model-level fence above proves the column is nullable with no default.
# It says nothing about what MIGRATION DAY does to the 19 orgs already in
# production. The statement below is the one the migration actually executes:
# it is lifted out of ``upgrade()`` by stubbing ``op``, not copied into the
# test, so a change to the migration reaches this fence instead of drifting
# past it. Precedent: tests/migrations/test_077_loan_backfill.py.


def _migration_081_backfill_sql() -> str:
    """The real ``op.execute`` argument from migration 081.

    ⚠ Lifted, not transcribed. A transcribed copy is a second source of truth
    for the exact thing this fence exists to check, and it would stay green
    after the migration changed.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "081_org_primary_currency.py"
    )
    spec = importlib.util.spec_from_file_location("_m081", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: dict = {}

    def _add_column(_table, column):
        captured["column"] = column

    def _execute(sql):
        captured["sql"] = sql

    module.op.add_column = _add_column
    module.op.execute = _execute
    module.upgrade()
    return captured["sql"], captured["column"]


def test_f6_the_migration_adds_the_column_with_no_server_default():
    """F6. Kills ``server_default="EUR"`` in the DDL rather than the model.

    The model fence above reads ``Organization.__table__``, which the
    migration does not touch. Both are asserted because they are two files and
    either one alone can carry the default.
    """
    _sql, column = _migration_081_backfill_sql()
    assert column.nullable is True
    assert column.server_default is None, (
        f"migration 081 adds primary_currency with "
        f"server_default={column.server_default!r}"
    )


def test_f6_the_backfill_leaves_legacy_multi_currency_orgs_null():
    """F6. Kills a backfill that picks A currency for a multi-currency org.

    Three shapes in one table, because the danger is a statement that gets one
    of them right. ``MIN(currency)`` without the ``HAVING COUNT(DISTINCT) = 1``
    guard would hand a legacy EUR+USD org the string ``'EUR'`` — which then
    scopes half its money out of every aggregate, silently, on migration day.
    """
    import sqlalchemy as sa

    sql, _column = _migration_081_backfill_sql()
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(sa.text(
            "CREATE TABLE organizations "
            "(id INTEGER PRIMARY KEY, primary_currency VARCHAR(3))"
        ))
        conn.execute(sa.text(
            "CREATE TABLE accounts "
            "(id INTEGER PRIMARY KEY, org_id INTEGER, currency VARCHAR(3))"
        ))
        conn.execute(sa.text("INSERT INTO organizations (id) VALUES (1),(2),(3),(4),(5)"))
        conn.execute(sa.text(
            "INSERT INTO accounts (org_id, currency) VALUES "
            "(1,'EUR'),(1,'EUR'),(1,'USD'),(3,'GBP'),"
            # 4: a single LOWERCASE legacy row. 5: case variants of one currency.
            "(4,'eur'),(5,'EUR'),(5,'eur')"
        ))
        conn.execute(sa.text(sql))
        rows = dict(conn.execute(sa.text(
            "SELECT id, primary_currency FROM organizations ORDER BY id"
        )).all())

    assert rows[1] is None, "legacy multi-currency org was given a currency"
    assert rows[2] is None, "zero-account org was given a currency"
    assert rows[3] == "GBP", (
        "single-currency org was NOT backfilled — the predicate would then be "
        "dead for every org that predates the door"
    )
    # ⚠ F14's SECOND HALF. ``accounts.currency`` was free text until PR 1, so
    # a legacy row can be lowercase. Without ``UPPER(TRIM(...))`` the backfill
    # stores ``'eur'`` verbatim, and the resolver then reports that org's only
    # currency as EXCLUDED. F14 covers the resolver; this covers the supplier,
    # and neither alone is enough — F14 sets ``primary_currency`` directly and
    # so cannot see the migration, while this runs no resolver.
    #
    # ⚠ It also removes a MySQL/SQLite divergence: SQLite's
    # ``COUNT(DISTINCT currency)`` is case-SENSITIVE while MySQL's
    # ``utf8mb4_0900_ai_ci`` folds, so org 5 below would be single-currency on
    # production and multi-currency here. Folding inside the COUNT makes both
    # engines agree, which is what lets this fence mean anything at all on the
    # aiosqlite shards.
    assert rows[4] == "EUR", (
        f"a lowercase legacy currency was stored raw as {rows[4]!r}; the org "
        "would then report its own currency as excluded, permanently"
    )
    assert rows[5] == "EUR", (
        "case variants of ONE currency must fold to one distinct value, or "
        "SQLite and MySQL disagree about whether the org is multi-currency"
    )


# ── F4: the SECOND account-insert site, closed ──────────────────────────────
#
# ⚠⚠ THIS IS THE LIVE DEFECT THE TICKET DID NOT KNOW ABOUT.
# ``assert_org_currency_allows`` guarded ONE of TWO ``Account`` insert sites.
# The other is ``demo_seed_service``, which hardcoded ``currency="EUR"``, and
# ``seed_org``'s refusal guards do not look at accounts at all: ``_has_real_data``
# counts TRANSACTIONS and ``_has_sentinel`` checks a CATEGORY slug. So PR 1
# closed one of two doors and nothing noticed for a month.
#
# ⚠ BUILT AS TWO ORDINARY API CALLS, DELIBERATELY. A service-level test that
# called ``seed_org`` directly would fence the same line of code and prove
# nothing about reachability — and reachability is the entire finding. These
# are the exact two requests an org owner makes during onboarding.


@pytest_asyncio.fixture
async def seed_world(engine_and_factory) -> dict:
    """One org, one owner, a checking type and the four system categories the
    demo seed needs. NO accounts — the tests create those through the API."""
    from app.models.user import Role, User
    from app.security import hash_password

    _engine, factory = engine_and_factory
    async with factory() as db:
        org = Organization(name="Seed Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="owner", email="owner@seed.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_active=True, email_verified=True,
        )
        db.add(user)
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at)
        for slug, name in (
            ("paycheck", "Paycheck"), ("groceries", "Groceries"),
            ("rent_mortgage", "Rent"), ("coffee_shops", "Coffee"),
        ):
            db.add(Category(
                org_id=org.id, name=name, slug=slug,
                is_system=True, type=CategoryType.BOTH,
            ))
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "at_id": at.id}


@pytest.fixture
def seed_client(engine_and_factory, seed_world):
    """accounts + onboarding mounted on one app, so the two calls are the two
    calls an org owner actually makes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.database import get_db
    from app.deps import get_current_user, get_session_factory
    from app.models.user import User
    from app.rate_limit import limiter
    from app.routers.accounts import router as accounts_router
    from app.routers.onboarding import router as onboarding_router

    _engine, factory = engine_and_factory
    limiter.reset()

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def _get_db():
        async with factory() as session:
            yield session

    async def _current_user():
        async with factory() as db:
            return (
                await db.execute(
                    select(User).where(User.id == seed_world["user_id"])
                )
            ).scalar_one()

    def _session_factory():
        return factory

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_session_factory] = _session_factory
    app.include_router(accounts_router)
    app.include_router(onboarding_router)

    with TestClient(app) as client:
        yield client, seed_world
    limiter.reset()


def _currencies_via_api(client) -> list[str]:
    """Read the org's currencies back through the API, not the ORM.

    Keeps these two tests entirely on the HTTP surface — which is the point of
    F4 — and avoids driving a second event loop at the aiosqlite engine the
    async fixtures own.
    """
    res = client.get("/api/v1/accounts")
    assert res.status_code == 200, res.text
    return sorted({a["currency"] for a in res.json()})


def test_f4_seed_demo_does_not_add_a_second_currency(seed_client):
    """F4. Kills the hardcoded ``currency="EUR"`` in ``demo_seed_service``.

    Two ordinary calls. Pre-fix this produced ``['EUR', 'USD']`` and a forecast
    ``executed_expense`` of 1215.30 that was a EUR+USD sum with no label —
    measured on real MySQL 8.4.11 over real HTTP, 2026-09-12.

    ⚠ ``accounts_created == 2`` is asserted alongside, and is load-bearing:
    without it, "refuse to seed whenever the org already has an account"
    satisfies this fence while breaking onboarding for anyone who created an
    account first. It is the same over-reach control the PR 1 file pairs with
    each of its two real fences.
    """
    client, seeds = seed_client

    created = client.post(
        "/api/v1/accounts",
        json={"name": "Dollar", "account_type_id": seeds["at_id"], "currency": "USD"},
    )
    assert created.status_code == 201, created.text

    seeded = client.post("/api/v1/users/me/onboarding/seed-demo")
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["accounts_created"] == 2, (
        "the seed refused instead of adapting — that closes the door by "
        "breaking onboarding"
    )
    assert seeded.json()["transactions_created"] > 0

    got = _currencies_via_api(client)
    assert got == ["USD"], f"the org acquired a second currency: {got}"


def test_f4_a_fresh_org_still_seeds_in_eur(seed_client):
    """⚠ OVER-REACH CONTROL, not a fence. Passes against today's unmodified code.

    Without it, hardcoding the seed to USD — or to whatever the last org used —
    satisfies F4 while making the default onboarding experience wrong for
    everyone who never creates an account first (which is most people, since
    the seed IS the first thing onboarding offers).
    """
    client, _seeds = seed_client

    seeded = client.post("/api/v1/users/me/onboarding/seed-demo")
    assert seeded.status_code == 200, seeded.text

    got = _currencies_via_api(client)
    assert got == ["EUR"], got


# ── F7: the LLM prompt's numerator and denominator in the SAME money ────────
#
# ⚠ WHY THIS ONE IS DIFFERENT FROM EVERY OTHER FENCE IN THIS FILE.
# ``ai_forecast_refine_service._build_category_history`` is a PYTHON bucket-sum
# over raw rows, not a ``func.sum``, so it is invisible to any grep-shaped
# inventory of aggregate sites — and it is the more dangerous of the two
# figures, because ``select_categories_by_scope(_spend_by_category(history),
# scope)`` uses the HISTORY to decide WHICH CATEGORIES ARE REFINED AT ALL.
#
# Scope the baseline and not the history and the model receives a scoped
# numerator against an unscoped denominator, inside ONE BILLED PROMPT, with no
# HTTP response and no React tree anywhere that a warning banner could reach
# (this consumer takes ``compute_forecast`` service-to-service with the live
# session — which is the whole reason ``currency_scope`` is in-band).
#
# ⚠ THE VACUITY TRAP HERE IS "ASSERT THE BASELINE IS SCOPED". That passes
# against the exact defect F7 exists to catch. The assertions below are on the
# HISTORY that reaches ``_build_refine_prompt``, and the USD money is parked on
# a category with NO EUR spending at all, so an unscoped history carries a
# category the scoped baseline reports zero for — the mismatch made visible.


@pytest_asyncio.fixture
async def refine_world(db) -> dict:
    """Two currencies, and each currency's HISTORY on its own category.

    The USD-only category is the probe: it can only appear in the prompt if the
    history was built unscoped.
    """
    bones = await _bones(db, primary_currency="EUR")
    eur = await _account(db, bones, "Main (EUR)", "EUR")
    usd = await _account(db, bones, "Dollar (USD)", "USD")
    usd_only = Category(
        org_id=bones["org"].id, name="Dollar Only", slug="dollar-only",
        type=CategoryType.EXPENSE,
    )
    db.add(usd_only)
    await db.flush()

    # Current period, so the BASELINE has something to be scoped.
    await _money(db, bones, eur, eur_leg=True)
    await _money(db, bones, usd, eur_leg=False)

    # History: strictly before P_START, which is the window
    # ``_build_category_history`` reads.
    for month in (4, 5):
        db.add(Transaction(
            org_id=bones["org"].id, account_id=eur.id,
            category_id=bones["food"].id,
            description="EUR history", amount=EUR_EXECUTED,
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=datetime.date(2026, month, 7),
            settled_date=datetime.date(2026, month, 7),
        ))
        db.add(Transaction(
            org_id=bones["org"].id, account_id=usd.id,
            category_id=usd_only.id,
            description="USD history", amount=USD_EXECUTED,
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=datetime.date(2026, month, 7),
            settled_date=datetime.date(2026, month, 7),
        ))
    await db.commit()
    return {**bones, "eur": eur, "usd": usd, "usd_only": usd_only}


async def test_f7_refine_scopes_the_history_and_the_baseline_together(
    db, refine_world, monkeypatch
):
    """F7. Kills scoping the BASELINE ONLY — and scoping neither.

    Driven through ``refine_forecast``, the real entry point, with the prompt
    builder wrapped to capture what it was actually handed. The LLM dispatch
    below it fails with ``NoRoutingConfigured`` and is swallowed into a
    baseline fallback, which is fine: the prompt is built before the dispatch,
    and the prompt is what this fence is about.

    ⚠ Three assertions, and the middle one is the fence. Asserting only that
    the baseline is scoped (the first) passes against the live hazard;
    asserting only that a total differs (the third) can be satisfied by a
    history that happens to be smaller. The USD-only category's ABSENCE is the
    assertion a baseline-only implementation cannot survive, because that
    category is what ``select_categories_by_scope`` would have put in front of
    the model.
    """
    from app.services import ai_forecast_refine_service as refine

    captured: dict = {}
    real_builder = refine._build_refine_prompt

    def _capture(**kwargs):
        captured.update(kwargs)
        return real_builder(**kwargs)

    monkeypatch.setattr(refine, "_build_refine_prompt", _capture)

    await refine.refine_forecast(
        db, org_id=refine_world["org"].id, period_start=P_START
    )

    assert captured, (
        "the prompt was never built — this fence saw nothing. Check the "
        "history window and that the fixture has pre-period actuals."
    )

    # 1. The baseline is scoped. NOT the fence — see the docstring.
    baseline = captured["baseline"]
    assert Decimal(baseline["executed_expense"]) == EUR_EXECUTED

    # 2. ⚠⚠ THE FENCE. The history is scoped WITH it.
    history = captured["history"]
    cat_ids = {row["category_id"] for row in history}
    assert refine_world["usd_only"].id not in cat_ids, (
        "the USD-only category reached the prompt: the history was built "
        "unscoped while the baseline was scoped, so the model is being billed "
        "to reason about a category the baseline reports as zero"
    )
    assert cat_ids == {refine_world["food"].id}, cat_ids

    # 3. And the totals agree with that.
    total = sum(Decimal(row["total_expense"]) for row in history)
    assert total == EUR_EXECUTED * 2, (
        f"history total {total}; the naive cross-currency total is "
        f"{(EUR_EXECUTED + USD_EXECUTED) * 2}"
    )


# ── F9-F13: the sites the fixtures ABOVE structurally cannot reach ──────────
#
# ⚠⚠ WHY THESE FIVE WERE UNFENCED WHILE THE SUITE WAS GREEN.
# A 2026-09-12 review neutered ``org_currency_filter`` at each of the sites
# below, one at a time, and the ENTIRE backend suite stayed green. The cause is
# structural, not an oversight: every other fixture in the repo leaves
# ``organizations.primary_currency`` NULL, so ``excluded_account_count`` is 0
# everywhere and the predicate short-circuits to ``true()``. No pre-existing
# test can tell scoped from unscoped — the two-currency fixture in this file is
# the only one in the tree.
#
# ⚠ THE GAP HAS ALREADY SHIPPED A DEFECT. ``populate_from_sources`` scoped its
# history and current-period sources and NOT its recurring one, so a
# cross-currency ``planned_amount`` was computed and PERSISTED — and that
# column is the DENOMINATOR of a verdict whose numerator is scoped. F11 exists
# to make a two-of-three fix red.
#
# The three sites above are each reached through a DIFFERENT window, so the
# fixture below parks three probe categories that cannot be confused:
#
#   ``food``   — the only category with a RECURRING template.
#   ``rent``   — history in April + May, and NOTHING in the period.
#   ``travel`` — in the period only, and NOTHING in history.
#
# A predicate dropped from one of the three windows therefore moves exactly one
# persisted number, which is what makes F11 a three-source fence rather than a
# totals-only one that a two-of-three fix survives.


async def _hist_expense(db, bones, acct, category, amount: Decimal) -> None:
    """One settled expense in each of ``HIST_MONTHS``, strictly before P_START."""
    for month in HIST_MONTHS:
        day = datetime.date(2026, month, 7)
        db.add(Transaction(
            org_id=bones["org"].id, account_id=acct.id, category_id=category.id,
            description="History", amount=amount,
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=day, settled_date=day,
        ))
    await db.flush()


@pytest_asyncio.fixture
async def with_history(db) -> dict:
    """``two_currency``, plus trailing history — and an OPEN June period.

    ⚠ THE MAGNITUDES ARE LOAD-BEARING, same argument as the module docstring.
    Two currencies is necessary and NOT sufficient: a fixture whose scoped and
    naive answers merely DIFFER passes against a broken implementation whenever
    both land in the same rendered band. Every number here is picked so the two
    answers land on opposite sides of something the user can see:

      ``rent``   400 over 3 months  ->  draft suggests    133.33  vs  4800.00
      ``food``   240 trailing, 100 in-period, 500 budgeted:
                 scoped  proj 100 -> HEADROOM 400 -> a rebalance is OFFERED
                 naive   proj 5100 -> headroom 0  -> ``empty_no_surplus``
      plan item  actual 100 vs 5100 against a 500 plan: variance -400 vs +4600,
                 i.e. UNDER plan vs OVER plan, a sign flip on the rendered bar.

    ⚠ THE PERIOD IS OPENED (``end_date = None``) ON PURPOSE.
    ``budget_rebalance_service.suggest_rebalance`` enters through
    ``get_current_period``, which is defined as "the row with ``end_date IS
    NULL``" and AUTO-CREATES one anchored to ``date.today()`` when there is
    none. Leaving June closed would hand that fence a wall-clock period row and
    an empty budget set. Nothing else moves: ``populate_from_sources`` derives
    ``p_end`` as ``p_start + 1 month - 1 day`` when ``end_date`` is NULL, which
    is P_END, and ``period_spend_window_end`` floors an open row at ``today``,
    which is P_END too.
    """
    bones = await _bones(db, primary_currency="EUR")
    eur = await _account(db, bones, "Main (EUR)", "EUR")
    usd = await _account(db, bones, "Dollar (USD)", "USD")

    rent = Category(
        org_id=bones["org"].id, name="Rent", slug="rent", type=CategoryType.EXPENSE
    )
    travel = Category(
        org_id=bones["org"].id, name="Travel", slug="travel",
        type=CategoryType.EXPENSE,
    )
    db.add_all([rent, travel])
    await db.flush()

    # In-period money on ``food`` / ``pay``, plus the recurring templates.
    await _money(db, bones, eur, eur_leg=True)
    await _money(db, bones, usd, eur_leg=False)

    await _hist_expense(db, bones, eur, bones["food"], EUR_FOOD_HIST)
    await _hist_expense(db, bones, usd, bones["food"], USD_FOOD_HIST)
    await _hist_expense(db, bones, eur, rent, EUR_RENT_HIST)
    await _hist_expense(db, bones, usd, rent, USD_RENT_HIST)

    for acct, amount in ((eur, EUR_TRAVEL_NOW), (usd, USD_TRAVEL_NOW)):
        db.add(Transaction(
            org_id=bones["org"].id, account_id=acct.id, category_id=travel.id,
            description="Flights", amount=amount,
            type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
            date=datetime.date(2026, 6, 8), settled_date=datetime.date(2026, 6, 8),
        ))

    db.add(Budget(
        org_id=bones["org"].id, category_id=bones["food"].id,
        amount=PLAN, period_start=P_START, period_end=P_END,
    ))

    period = (
        await db.execute(
            select(BillingPeriod).where(BillingPeriod.org_id == bones["org"].id)
        )
    ).scalar_one()
    period.end_date = None

    await db.commit()
    return {**bones, "eur": eur, "usd": usd, "rent": rent, "travel": travel}


# ── F9: the reports compiler ────────────────────────────────────────────────


async def test_f9_the_reports_transactions_source_is_scoped(db, with_history):
    """F9. Kills the predicate removed from ``compile_ast_to_query``.

    Driven through ``execute_query``, never the compiler, for the reason the
    budget and spending fences above give: ``compile_ast_to_query`` takes
    ``currency_scope`` as a PARAMETER defaulting to ``None``, so compiling a
    statement by hand with a scope the TEST supplied proves only that the
    helper works — not that any real entry point resolves one.

    ⚠ TWO category rows are asserted, not a grand total. The clause sits on the
    WHERE of a GROUPED statement; a total-only assertion can be satisfied by a
    coincidence of offsetting rows, and the reports surface renders the rows.

    ⚠ The transactions source is the one dataset of the four whose catalog
    publishes NO currency dimension and NO currency filter, so the user cannot
    separate the money by hand either.
    """
    rows, _meta = await reports_query_service.execute_query(
        db,
        ReportsQuery(
            dataset=Dataset.TRANSACTIONS,
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
            dimensions=[Dimension.CATEGORY],
        ),
        org_id=with_history["org"].id,
    )
    by_cat = {r["category"]: Decimal(str(r["value"])) for r in rows}

    scoped_food = EUR_EXECUTED + EUR_PENDING + EUR_FOOD_HIST * len(HIST_MONTHS)
    assert by_cat["Food"] == scoped_food, (
        f"Food sums to {by_cat['Food']}; the naive cross-currency sum is "
        f"{scoped_food + USD_EXECUTED + USD_PENDING + USD_FOOD_HIST * 2}"
    )
    scoped_rent = EUR_RENT_HIST * len(HIST_MONTHS)
    assert by_cat["Rent"] == scoped_rent, (
        f"Rent sums to {by_cat['Rent']}; the naive cross-currency sum is "
        f"{scoped_rent + USD_RENT_HIST * 2}"
    )


# ── F10: the plan's batched actuals ─────────────────────────────────────────


async def test_f10_plan_actuals_are_scoped(db, with_history):
    """F10. Kills the predicate removed from ``_compute_actuals_batch``.

    Driven through ``get_plan_for_period``, because ``_compute_actuals_batch``
    takes ``currency_scope`` as a parameter defaulting to ``None`` — the same
    shape, and the same reason, as every other threaded site in this file.

    ⚠ THE VARIANCE IS THE ASSERTION THAT MATTERS. ``variance = actual -
    planned`` renders as an under/over-plan bar, and 500 planned against a
    scoped 100 gives **-400 (under)** while the naive 5100 gives **+4600
    (over)**. A sign flip, not a wrong magnitude — the same class of failure as
    F3's verdict-boundary assertion, and the reason this fence does not stop at
    ``actual_amount``.
    """
    org_id = with_history["org"].id
    plan = await forecast_plan_service.get_or_create_plan(
        db, org_id, P_START, today=TODAY
    )
    from app.schemas.forecast_plan import ForecastPlanItemCreate

    await forecast_plan_service.upsert_item(
        db, org_id, plan.id,
        ForecastPlanItemCreate(
            category_id=with_history["food"].id, type="expense",
            planned_amount=PLAN,
        ),
        today=TODAY,
    )

    resp = await forecast_plan_service.get_plan_for_period(
        db, org_id, P_START, today=TODAY
    )
    item = next(i for i in resp.items if i.category_id == with_history["food"].id)

    assert Decimal(item.actual_amount) == EUR_EXECUTED, (
        f"the plan actual is {item.actual_amount}; the naive cross-currency "
        f"sum is {EUR_EXECUTED + USD_EXECUTED}"
    )
    assert Decimal(item.variance) < 0, (
        f"variance is {item.variance}: the item renders OVER plan on money the "
        f"scope should have excluded"
    )
    assert Decimal(resp.total_actual_expense) == EUR_EXECUTED


# ── F11: populate, and ALL THREE of its sources ─────────────────────────────


async def test_f11_populate_scopes_all_three_sources(db, with_history):
    """F11. Kills the predicate removed from ANY ONE of populate's three selects.

    ⚠⚠ THIS IS THE FENCE THE SHIPPED DEFECT NEEDED. The history and
    current-period sources were scoped and the RECURRING one was not, so
    ``planned_amount`` was written as a cross-currency total and PERSISTED. A
    fence covering two of the three is exactly the hole that shipped, which is
    why each source is asserted through its own probe category:

        ``food``   RECURRING select   25.00  vs naive  3025.00
        ``rent``   history select    200.00  vs naive  7200.00
        ``travel`` current select      60.00  vs naive  6060.00

    ⚠ ASSERTED ON THE PERSISTED ROW, not on the response. The response is
    re-rendered from a freshly resolved scope on every read, so a scoped
    response can sit on top of an unscoped stored number indefinitely — and the
    stored number is the one that becomes ``total_planned_expense``, the
    denominator of OnTrackTile's verdict.

    ⚠ ``food`` carries history and in-period money too, and gets a RECURRING
    item anyway: the recurring loop runs first and claims ``(food, expense)``
    in ``existing_keys``. That is load-bearing, not incidental — it is what
    isolates the recurring select from the other two.

    ⚠ ``source`` is asserted alongside each amount. Without it, an
    implementation that dropped a source entirely (and let another window
    supply the category) would satisfy the amounts by accident.
    """
    org_id = with_history["org"].id
    await forecast_plan_service.populate_from_sources(
        db, org_id, P_START, today=TODAY
    )

    rows = (
        await db.execute(
            select(ForecastPlanItem).where(ForecastPlanItem.org_id == org_id)
        )
    ).scalars().all()
    by_cat = {r.category_id: r for r in rows}

    food = by_cat[with_history["food"].id]
    assert food.source is ItemSource.RECURRING
    assert food.planned_amount == EUR_RECURRING, (
        f"the RECURRING source persisted {food.planned_amount}; the naive "
        f"cross-currency total is {EUR_RECURRING + USD_RECURRING}. This is the "
        f"defect that shipped."
    )

    rent = by_cat[with_history["rent"].id]
    assert rent.source is ItemSource.HISTORY
    assert rent.planned_amount == EUR_RENT_HIST, (
        f"the HISTORY source persisted {rent.planned_amount}; the naive "
        f"cross-currency monthly average is {EUR_RENT_HIST + USD_RENT_HIST}"
    )

    travel = by_cat[with_history["travel"].id]
    assert travel.source is ItemSource.HISTORY
    assert travel.planned_amount == EUR_TRAVEL_NOW, (
        f"the CURRENT-PERIOD source persisted {travel.planned_amount}; the "
        f"naive cross-currency total is {EUR_TRAVEL_NOW + USD_TRAVEL_NOW}"
    )


# ── F12: the budget draft ───────────────────────────────────────────────────


async def test_f12_the_budget_draft_averages_one_currency(db, with_history):
    """F12. Kills the predicate removed from ``_gather_draft_facts``.

    Driven through ``suggest_next_period_budget`` — ``_gather_draft_facts``
    takes ``currency_scope`` as a parameter defaulting to ``None``.

    ``rent`` is the probe because ``food`` already HAS a budget in the target
    period and the draft skips budgeted categories by design. Its suggestion is
    ``400 / 3 = 133.33``; unscoped it is ``14400 / 3 = 4800.00``, a budget
    thirty-six times the size of the one the user should be offered, written
    into a form they are invited to accept with one click.

    ⚠ ``_gather_draft_facts`` reads ``datetime.date.today()`` directly — it is
    the one site in this block with no injectable clock. It is not a wall-clock
    date bomb HERE, and the reason is worth stating: the window's upper bound
    is ``min(current_month_start, period_start)``, and ``period_start`` is
    2026-06-01, so once the wall clock is past June 2026 the bound is pinned to
    ``period_start`` forever. It would only drift if the suite ran BEFORE that
    date.
    """
    res = await budget_draft_service.suggest_next_period_budget(
        db, with_history["org"].id, period_start=P_START
    )
    assert res.status == "ok", res.summary

    by_cat = {s.category_id: s for s in res.suggestions}
    assert with_history["food"].id not in by_cat, (
        "food already has a budget in the target period and must not be drafted"
    )
    rent = by_cat[with_history["rent"].id]
    scoped = (EUR_RENT_HIST * len(HIST_MONTHS) / Decimal(3)).quantize(
        Decimal("0.01")
    )
    naive = (
        (EUR_RENT_HIST + USD_RENT_HIST) * len(HIST_MONTHS) / Decimal(3)
    ).quantize(Decimal("0.01"))
    assert rent.suggested_amount == scoped, (
        f"the draft suggests {rent.suggested_amount}; the naive "
        f"cross-currency average is {naive}"
    )


# ── F13: the rebalance, and BOTH sums off its one shared filter ─────────────


async def test_f13_the_rebalance_scopes_both_of_its_sums(
    db, with_history, monkeypatch
):
    """F13. Kills the predicate removed from ``_gather_facts``' ``base_filter``.

    That one list feeds BOTH the 3-month rollup and the current-period rollup,
    so a single deletion moves two numbers — and the assertions below name them
    separately because the shared list is a REFACTOR, not a guarantee: the next
    person to split it back into two ``.where()`` calls needs a fence that can
    see which half they dropped.

    ⚠ THE STATUS ASSERTION IS THE VERDICT-BOUNDARY ONE, and it comes first
    because it is also what makes the rest reachable. Scoped, ``food`` projects
    at 100 against a 500 budget: headroom 400, the rebalance is offered.
    Unscoped it projects at 5100, ``total_headroom`` hits 0 and the service
    RETURNS EARLY with ``empty_no_surplus`` — a user-visible refusal
    ("everything is over budget") manufactured entirely out of another
    currency's money, and before ``_build_messages`` is ever called.

    The facts are read off ``_build_messages`` rather than the response for the
    same reason F7 reads the refine prompt: the aggregates are what is being
    fenced, and the allocator downstream is free to emit no suggestions at all
    when nothing is in deficit.

    ⚠ The LLM dispatch below ``_build_messages`` fails with
    ``NoRoutingConfigured`` and is swallowed into the deterministic allocator.
    That is fine and deliberate — the prompt is built before the dispatch.
    """
    captured: dict = {}
    real_builder = budget_rebalance_service._build_messages

    def _capture(facts, period_start):
        captured["facts"] = facts
        return real_builder(facts, period_start)

    monkeypatch.setattr(budget_rebalance_service, "_build_messages", _capture)

    res = await budget_rebalance_service.suggest_rebalance(
        db, org_id=with_history["org"].id, today=TODAY
    )
    assert res.status == "ok", (
        f"status is {res.status!r}: an unscoped current-period sum projects "
        f"food at {EUR_EXECUTED + USD_EXECUTED} against a {PLAN} budget, which "
        f"drives total_headroom to 0 and refuses the rebalance outright"
    )

    fact = next(
        f for f in captured["facts"]
        if f.category_id == with_history["food"].id
    )
    assert fact.last_3mo_total == EUR_FOOD_HIST * len(HIST_MONTHS), (
        f"the 3-month rollup is {fact.last_3mo_total}; the naive "
        f"cross-currency total is "
        f"{(EUR_FOOD_HIST + USD_FOOD_HIST) * len(HIST_MONTHS)}"
    )
    assert fact.current_mo_actual == EUR_EXECUTED, (
        f"the current-period rollup is {fact.current_mo_actual}; the naive "
        f"cross-currency total is {EUR_EXECUTED + USD_EXECUTED}"
    )


async def test_g2_the_five_new_sites_are_unchanged_for_a_single_currency_org(
    db, single_currency
):
    """⚠ OVER-REACH CONTROL, not a fence. Passes against today's unmodified code.

    Same posture as ``G1`` above and ``test_f4_a_fresh_org_still_seeds_in_eur``.
    Every assertion in F9-F13 is satisfied by "return nothing whenever a second
    currency exists anywhere", or by scoping the 99% of orgs that have nothing
    to exclude down to zero. This says the ordinary org still gets its money.

    It cannot fail against a broken scoping implementation UNLESS that
    implementation also breaks the ordinary path — which is exactly its job,
    and why it is labelled rather than counted as coverage.
    """
    org_id = single_currency["org"].id

    rows, meta = await reports_query_service.execute_query(
        db,
        ReportsQuery(
            dataset=Dataset.TRANSACTIONS,
            measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
            dimensions=[Dimension.CATEGORY],
        ),
        org_id=org_id,
    )
    by_cat = {r["category"]: Decimal(str(r["value"])) for r in rows}
    assert by_cat["Food"] == EUR_EXECUTED + EUR_PENDING
    assert meta["warning"] is None, meta["warning"]

    await forecast_plan_service.populate_from_sources(
        db, org_id, P_START, today=TODAY
    )
    rows = (
        await db.execute(
            select(ForecastPlanItem).where(ForecastPlanItem.org_id == org_id)
        )
    ).scalars().all()
    food = next(r for r in rows if r.category_id == single_currency["food"].id)
    assert food.source is ItemSource.RECURRING
    assert food.planned_amount == EUR_RECURRING


# ── F14 — a legacy LOWERCASE currency must not exclude itself ──────────────


@pytest_asyncio.fixture
async def lowercase_legacy(db) -> dict:
    """One org, ONE account, currency stored as ``'eur'``.

    Reachable: ``accounts.currency`` was free text until TBD-325 PR 1, and
    migration 081 backfills ``primary_currency`` from whatever those rows hold.
    Nothing in the product can repair it afterwards -- currency is immutable
    post-create (``AccountUpdate`` has no field for it) and
    ``primary_currency``'s single writer only fires when the org has ZERO
    accounts.
    """
    bones = await _bones(db, primary_currency="eur")
    acct = await _account(db, bones, "Legacy", "eur")
    await _money(db, bones, acct, eur_leg=True)
    await db.commit()
    return {**bones, "acct": acct}


@pytest.mark.asyncio
async def test_f14_a_lowercase_single_currency_org_excludes_nothing(
    db, lowercase_legacy
):
    """F14. Kills: comparing a NORMALISED account currency against a RAW
    ``primary_currency`` in ``resolve_currency_scope``.

    Under that implementation ``normalise_currency('eur') != 'eur'`` is True,
    so the org's one and only currency is reported as EXCLUDED. The money stays
    arithmetically right; the UI lies about it, permanently:

    * ``OnTrackTile`` sees ``excluded_account_count > 0`` and drops its verdict
      on every render, printing "Covers your EUR accounts only. 1 account in
      eur is not included" -- naming the same currency on both sides.
    * Sankey and Reports emit a multi-currency warning for one currency.
    * Every aggregate grows the correlated subquery the short-circuit exists to
      avoid, so F1's guarantee silently stops holding.

    ⚠ This asserts the SCOPE, not a sum. The sums are unaffected on MySQL
    (``utf8mb4_0900_ai_ci`` matches ``= 'eur'`` case-insensitively), which is
    exactly why a figure-based fence cannot see this defect and why the
    assertion below is on ``excluded_account_count``.
    """
    scope = await currency_service.resolve_currency_scope(
        db, org_id=lowercase_legacy["org"].id
    )
    assert scope["excluded_account_count"] == 0, (
        f"a single-currency org reported itself as excluded: {scope}"
    )
    assert scope["excluded_currencies"] == []
    # Normalised on the way out, so every consumer sees one spelling.
    assert scope["currency"] == "EUR"
