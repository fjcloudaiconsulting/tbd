"""TBD-221 — ``GET /api/v1/transactions/spending-by-category``, the UNGATED
historical-actuals rollup the dashboard Spending-by-Category donut consumes.

Design note: ``specs/2026-08-05-spending-donut-server-rollup.md``. That note
routes the donut onto the per-category rollup that lives inside
``GET /api/v1/forecast``. TBD-197 then gave every org a Forecast opt-out that
404s that route — so the donut, a *historical actuals* tile, would go blank
over a period holding real settled expense. This endpoint is the extraction:
the same actuals rollup, on the transactions router, with no feature gate.

**Why the transactions router and not ``/forecast/...``.**
``frontend/tests/components/dashboard/dashboard-forecast-fetch-skip.test.tsx``
asserts that a forecast-off org issues **no** ``/api/v1/forecast*`` request
except ``account-balances``. Mounting this under the forecast prefix would
redden that fence or force it to be weakened into a two-exception rule, and
``app/routers/forecast.py``'s module docstring exists precisely to hold the
one-exception rule shut.

Every fence below **names the row it writes** — the rule
``tests/test_forecast_toggle.py`` established for TBD-197. No test here says
merely "forecast off"; each writes an explicit
``OrgSetting(org, "orgpref.forecast", "off")`` and says so.

Fences:

* ``F-A`` — the point of the ticket. ONE org, ONE
  ``OrgSetting(org, "orgpref.forecast", "off")`` row, real settled reportable
  expense in the period: ``GET /api/v1/forecast`` → **404** while
  ``GET /api/v1/transactions/spending-by-category`` → **200 with non-empty
  categories**. Mutant killed: the ``Feature.FORECAST`` dependency applied to
  the new route (or the route mounted on the forecast router, which is the
  same mistake wearing a URL).
* ``F-B`` — the extraction SHARES the query rather than copying it. On a period
  holding **more than 200 rows**, ``sum(row.executed)`` from this endpoint
  equals ``/api/v1/forecast``'s ``executed_expense``, **and** ``period_end`` is
  the same date from both. Mutants killed: a duplicated rollup that drifts, and
  two independently-derived spend windows.
* ``F-C`` — ``compute_forecast``'s response is byte-identical to ``main``'s.
  Two snapshots, one per arm of the window derivation being moved (a CLOSED
  period, and an OPEN period whose derived end is FLOORED at a later clock).
  ⚠ Recorded by running against ``main``'s sources, then **replayed** against
  them with this file kept — see the module footnote.
* ``F-D`` — grouped by ``category_id``, never by name. The fixture is a master
  and a subcategory that carry the **same name**, both with direct rows: under
  a name-grouping implementation they collapse into one row, under the real one
  they are two rows with two ids.
* ``F-E`` — the excluded row kinds, each fenced **on its own category** so a
  failure names the kind. ``is_manual_adjustment`` alone is the half-fix: it is
  the only one already on the wire, so an implementation that handles only it
  looks right and still counts REJECTED rows. Measured: under that half-fix the
  adjustment test stays GREEN while the rejected and outgoing-leg tests go red,
  which is exactly why a fence covering the adjustment alone is not a fence.
  A fifth kind (settled income) was added after the incoming-leg test was
  measured over-determined — see its docstring.
* ``F-F`` — control. An org with income but **no expense** answers **200** with
  ``categories: []`` and ``executed_expense: "0"`` — not a 404, not a 500.
* ``F-G`` — the **TENANT boundary**. ⚠ Every seeder for F-A … F-F calls
  ``_base_org`` exactly ONCE, so all of them run against a single organization
  whose id is 1: the correct implementation and a LEAKING one agree, because
  there is nothing to leak. F-G seeds a decoy org that holds real settled
  expense in the same window, plus one subject-org row carrying the decoy's
  ``category_id``. Mutants killed: ``Transaction.org_id == org_id`` dropped
  from the rollup, and ``Category.org_id == org_id`` dropped from
  ``load_category_meta`` — the second changes no number, only a label.
* ``F-H`` — the route **requires authentication**. ⚠ Every other fence here
  builds its app with ``current_user=``, which OVERRIDES ``get_current_user``
  outright, so none of them can see the route's auth dependency at all. F-H
  builds the app without the override: anonymous → 403, undecodable bearer →
  401. Mutant killed: the auth dependency deleted from the handler.
* ``F-I`` — the **substituted period**, and the fact that this GET can COMMIT.
  A ``period_start`` matching no row is silently replaced by the current
  period, and ``get_current_period`` may AUTO-CREATE one (TBD-297). Fences
  IDEMPOTENCE — two calls, one period row — and deliberately **not** the
  write, which would bless write-on-GET as contract.
* ``F-J`` — the two endpoint paths F-A … F-I never take: an **OPEN** period
  (whose ``period_end`` is floored at today — every other fixture here is
  closed) and an **OMITTED** ``period_start`` (every other test sends one).
  The live dashboard renders an open period, so this was the arm the product
  actually uses going untested at the endpoint level.

All dates in F-A … F-I are fixed literals and every clock-sensitive call is
given an explicit ``today=``; nothing there is ``date.today()``-relative
(``reference_wall_clock_date_bomb_tests``). **F-J is the exception, and has to
be**: an open period's window end IS the clock and the endpoint takes no
``today=`` over HTTP, so its fixture is anchored to ``today ± n`` and never to
a calendar literal.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Account, AccountType, Category, Organization
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.settings import OrgSetting
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.forecast import router as forecast_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services import forecast_service
from app.services.feature_gate import Feature, org_preference_key
from tests.factories import make_test_app


P_START = date(2026, 1, 1)
P_END = date(2026, 1, 31)

SPEND_URL = "/api/v1/transactions/spending-by-category"
FORECAST_URL = "/api/v1/forecast"

# F-B: the row count past which the client-side donut this replaces silently
# truncated (``routers/transactions.py`` caps ``limit`` at 200).
CAP = 200


# ── infra ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory():
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
        yield factory
    finally:
        await engine.dispose()


async def _base_org(
    db, *, name: str, username: str = "owner"
) -> tuple[Organization, User, Account]:
    """One org + owner + default account.

    ``username`` is a parameter because ``users.username`` is **globally**
    unique, not org-scoped: F-G is the first fixture here that seeds a SECOND
    organization, and the default would collide on the insert.
    """
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(
        org_id=org.id, username=username, email=f"owner@{org.id}.example",
        password_hash=hash_password("pw-1234567"), role=Role.OWNER,
        is_active=True, email_verified=True,
    )
    at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
    db.add_all([user, at])
    await db.flush()
    acct = Account(
        org_id=org.id, name="Acct", account_type_id=at.id,
        balance=Decimal("1000.00"), currency="EUR", is_default=True,
    )
    db.add(acct)
    await db.flush()
    return org, user, acct


def _tx(org_id, acct_id, cat_id, desc, amount, *, day, month=1,
        tx_type=TransactionType.EXPENSE, status=TransactionStatus.SETTLED,
        settled_day=None, **kw) -> Transaction:
    """One transaction on a fixed 2026 date.

    SETTLED rows always carry a ``settled_date`` — the flush-time listener
    ``_enforce_settled_implies_settled_date`` and the DB CHECK
    ``ck_transactions_settled_implies_settled_date`` both require it.
    """
    d = date(2026, month, day)
    return Transaction(
        org_id=org_id, account_id=acct_id, category_id=cat_id,
        description=desc, amount=Decimal(amount), type=tx_type, status=status,
        date=d,
        settled_date=(
            date(2026, month, settled_day) if settled_day is not None
            else (d if status is TransactionStatus.SETTLED else None)
        ),
        **kw,
    )


def _app(factory, user_id: int) -> FastAPI:
    async def _resolve(f) -> User:
        async with f() as db:
            return await db.get(User, user_id)

    return make_test_app(
        factory,
        routers=[transactions_router, forecast_router],
        current_user=_resolve,
    )


async def _write_org_row(factory, org_id: int, key: str, value: str) -> None:
    async with factory() as db:
        db.add(OrgSetting(org_id=org_id, key=key, value=value))
        await db.commit()


# ── F-A — the ticket: the gate closes /forecast and NOT this route ─────────


async def _seed_gate_org(factory) -> dict:
    """One org, one CLOSED January period, real settled reportable expense."""
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Gate Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        food = Category(org_id=org.id, name="Food", slug="food",
                        type=CategoryType.EXPENSE)
        db.add(food)
        await db.flush()
        db.add_all([
            _tx(org.id, acct.id, food.id, "groceries", "120.50", day=5),
            _tx(org.id, acct.id, food.id, "market", "9.50", day=20),
        ])
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "food_id": food.id}


@pytest.mark.asyncio
async def test_fa_forecast_opt_out_closes_forecast_and_leaves_spending_open(
    session_factory,
):
    """F-A. Writes ``OrgSetting(org, "orgpref.forecast", "off")`` — and nothing
    else — for an org holding 130.00 of settled reportable expense in January.

    Observes BOTH routes for THAT org in ONE test:

      ``GET /api/v1/forecast``                              → **404**
      ``GET /api/v1/transactions/spending-by-category``      → **200**, with a
      non-empty ``categories`` carrying the real 130.00.

    Mutant killed: a ``Depends(require_feature(Feature.FORECAST))`` on the new
    handler (or on the transactions router), which is the reflex edit for
    anything that says "rollup" and reads like forecast code. Under it the
    donut goes blank over a period with real spend — the defect this endpoint
    exists to remove — and no other test in the suite notices.
    """
    seed = await _seed_gate_org(session_factory)
    await _write_org_row(
        session_factory, seed["org_id"], org_preference_key(Feature.FORECAST), "off"
    )
    app = _app(session_factory, seed["user_id"])
    with TestClient(app) as client:
        gated = client.get(f"{FORECAST_URL}?period_start={P_START}")
        spend = client.get(f"{SPEND_URL}?period_start={P_START}")

    assert gated.status_code == 404, gated.text
    assert spend.status_code == 200, spend.text
    body = spend.json()
    # Not merely "not 404": a real payload with the real number, so a route
    # that answered 200 with an empty rollup could not pass.
    assert body["categories"] != []
    assert len(body["categories"]) == 1
    assert body["categories"][0]["category_id"] == seed["food_id"]
    assert Decimal(body["categories"][0]["executed"]) == Decimal("130.00")
    assert Decimal(body["executed_expense"]) == Decimal("130.00")


@pytest.mark.asyncio
async def test_fa_control_no_orgpref_row_keeps_both_open(session_factory):
    """F-A control. **No** ``orgpref.forecast`` row → both routes 200, and the
    two rollups carry the same number.

    Without this, an endpoint that 200s unconditionally *and* a ``/forecast``
    that 404s unconditionally would both pass F-A.
    """
    seed = await _seed_gate_org(session_factory)
    app = _app(session_factory, seed["user_id"])
    with TestClient(app) as client:
        forecast = client.get(f"{FORECAST_URL}?period_start={P_START}")
        spend = client.get(f"{SPEND_URL}?period_start={P_START}")

    assert forecast.status_code == 200, forecast.text
    assert spend.status_code == 200, spend.text
    assert Decimal(spend.json()["executed_expense"]) == Decimal(
        forecast.json()["executed_expense"]
    )


# ── F-B — one query and one window, shared, past 200 rows ──────────────────

HOME_ROWS = 90    # @ 1.00
UTIL_ROWS = 80    # @ 2.00  (subcategory of Home)
FOOD_ROWS = 80    # @ 3.00
FEB_TAIL_ROWS = 4  # @ 11.00, on Feb 1-4
BULK_EXECUTED = Decimal("534.00")   # 90*1 + 80*2 + 80*3 + 4*11

# ⚠ The period is a REAL billing cycle, 2026-01-05 → 2026-02-10, and it is
# deliberately NOT calendar-aligned AND NOT one month long. Both halves are
# load-bearing, and both were MEASURED rather than reasoned:
#
#   * A calendar-aligned January period (01-01 → 01-31) makes the mutant
#     "calendar month end of period_start" produce the SAME date, so F-B's
#     ``period_end`` equality passes against it.
#   * 01-05 → 02-04 fixes that one and STILL passes the mutant
#     "period_start + 1 month - 1 day", because a 1-month-long cycle is
#     exactly what that formula reproduces. That mutant was measured
#     SURVIVING the 02-04 fixture, all fourteen tests green.
#
# 37 days kills both, and the four February rows past 02-04 put the drift in
# the TOTALS too, not only in the ``period_end`` string.
B_START = date(2026, 1, 5)
B_END = date(2026, 2, 10)


async def _seed_bulk_org(factory) -> dict:
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Bulk Org")
        db.add(BillingPeriod(org_id=org.id, start_date=B_START, end_date=B_END))
        home = Category(org_id=org.id, name="Home", slug="home",
                        type=CategoryType.EXPENSE)
        food = Category(org_id=org.id, name="Food", slug="food",
                        type=CategoryType.EXPENSE)
        salary = Category(org_id=org.id, name="Salary", slug="salary",
                          type=CategoryType.INCOME)
        travel = Category(org_id=org.id, name="Travel", slug="travel",
                          type=CategoryType.EXPENSE)
        db.add_all([home, food, salary, travel])
        await db.flush()
        util = Category(org_id=org.id, name="Utilities", slug="utilities",
                        type=CategoryType.EXPENSE, parent_id=home.id)
        db.add(util)
        await db.flush()

        rows: list[Transaction] = []
        for i in range(HOME_ROWS):
            rows.append(_tx(org.id, acct.id, home.id, f"home-{i}", "1.00",
                            day=(i % 26) + 6))
        for i in range(UTIL_ROWS):
            rows.append(_tx(org.id, acct.id, util.id, f"util-{i}", "2.00",
                            day=(i % 26) + 6))
        for i in range(FOOD_ROWS):
            rows.append(_tx(org.id, acct.id, food.id, f"food-{i}", "3.00",
                            day=(i % 26) + 6))
        # The February tail, on 02-06..02-09: inside the real window
        # (ends 02-10), outside BOTH naive re-derivations of it (which end
        # 01-31 and 02-04 respectively).
        for i in range(FEB_TAIL_ROWS):
            rows.append(_tx(org.id, acct.id, food.id, f"feb-{i}", "11.00",
                            month=2, day=i + 6))
        # Income and a PENDING expense, so an endpoint that lost the type
        # filter or the status filter cannot land on the same total.
        rows.append(_tx(org.id, acct.id, salary.id, "pay", "5000.00", day=25,
                        tx_type=TransactionType.INCOME))
        rows.append(_tx(org.id, acct.id, travel.id, "flight", "700.00", day=13,
                        status=TransactionStatus.PENDING, settled_day=13))
        # ⚠ The three non-reportable kinds. Also load-bearing, also measured:
        # ``/forecast``'s ``executed_expense`` is a SEPARATE scalar query that
        # applies ``reportable_transaction_filter()``. Without rows the two
        # queries can disagree ABOUT, a rollup that drifted off that filter
        # still sums to the same number and F-B's equality passes.
        rows.append(_tx(org.id, acct.id, home.id, "adjustment", "700.00",
                        day=9, is_manual_adjustment=True))
        rows.append(_tx(org.id, acct.id, food.id, "rejected", "1100.00",
                        day=10, reconciliation_state="rejected"))
        db.add_all(rows)
        await db.flush()
        leg_out = _tx(org.id, acct.id, util.id, "leg out", "1300.00", day=11)
        leg_in = _tx(org.id, acct.id, food.id, "leg in", "1300.00", day=11,
                     tx_type=TransactionType.INCOME)
        db.add_all([leg_out, leg_in])
        await db.flush()
        leg_out.linked_transaction_id = leg_in.id
        leg_in.linked_transaction_id = leg_out.id
        await db.commit()
        return {"org_id": org.id, "user_id": user.id}


@pytest_asyncio.fixture
async def bulk(session_factory):
    seed = await _seed_bulk_org(session_factory)
    app = _app(session_factory, seed["user_id"])
    with TestClient(app) as c:
        yield c, seed


def test_fb_fixture_exceeds_the_200_row_cap(bulk):
    """The fixture IS half the contribution: pin its size so a later shrink
    cannot quietly turn F-B into a duplicate of the existing sub-200 coverage.
    """
    client, _ = bulk
    res = client.get(
        f"/api/v1/transactions?date_from={B_START}&date_to={B_END}&limit=1"
    )
    assert res.status_code == 200, res.text
    assert res.json()["total"] > CAP


def test_fb_rollup_agrees_with_forecast_past_200_rows(bulk):
    """F-B. Forecast is **ON** (no ``orgpref.forecast`` row is written by this
    fixture at all), the period holds 256 reportable rows plus four non-reportable ones.

    Asserts, across the two endpoints:

      ``sum(spending.categories[].executed) == forecast.executed_expense``
      ``spending.period_end == forecast.period_end``

    Mutants killed: the extraction that COPIES the ``group_by`` SQL instead of
    sharing it (the two can then drift while both look right), and a second,
    independently-derived spend window. Both are internally consistent, so
    neither endpoint's own tests would notice.
    """
    client, _ = bulk
    spend = client.get(f"{SPEND_URL}?period_start={B_START}")
    forecast = client.get(f"{FORECAST_URL}?period_start={B_START}")
    assert spend.status_code == 200, spend.text
    assert forecast.status_code == 200, forecast.text
    s, f = spend.json(), forecast.json()

    assert sum(Decimal(r["executed"]) for r in s["categories"]) == Decimal(
        f["executed_expense"]
    )
    assert s["period_end"] == f["period_end"]
    assert s["period_start"] == f["period_start"]
    # Pinned absolutely as well: an equality between two values that BOTH
    # drifted the same way is not a fence.
    assert s["period_start"] == B_START.isoformat()
    assert s["period_end"] == B_END.isoformat()

    # Absolute too, so a shared query that lost the SAME rows on both sides
    # would still fail.
    assert Decimal(f["executed_expense"]) == BULK_EXECUTED
    # ...and the endpoint's own total is the sum of its own rows, so the
    # donut's centre figure can never disagree with its slices.
    assert Decimal(s["executed_expense"]) == BULK_EXECUTED


def test_fb_spending_carries_executed_only(bulk):
    """F-B, second half of C3: the payload carries ``executed`` and NOTHING
    projected. ``pending`` / ``recurring`` / ``forecast`` are synthesized from
    templates that have not materialised — that IS the Forecast product, and
    re-exporting it here would re-gate this surface by the back door.

    The fixture's PENDING 700.00 flight is the probe: its category must not
    appear at all, and no projected key may exist on any row.

    ⚠ **The two assertion families below do different jobs, and only one of
    them reaches the service.** ``set(body)`` and ``set(row)`` pin the
    **SCHEMA**: those key sets are produced by Pydantic from the route's
    ``response_model``, so a service that started emitting ``pending`` would
    have it stripped silently on the way out and this half would still pass.
    That is the framework-injects-what-you-assert shape
    (``reference_sprint6_vacuity_classes``); it is kept because it does catch a
    ``response_model`` that GREW a projected field, which is the likelier edit.
    The half that pins the **SERVICE** is ``"Travel" not in {...}`` — the
    PENDING flight's category, a value no schema can add or remove. Do not read
    the key-set assertion as evidence that the service emits nothing projected.
    """
    client, _ = bulk
    body = client.get(f"{SPEND_URL}?period_start={B_START}").json()
    assert set(body) == {
        "period_start", "period_end", "executed_expense", "categories",
    }
    for row in body["categories"]:
        assert set(row) == {
            "category_id", "category_name", "parent_id", "executed",
        }
    assert "Travel" not in {r["category_name"] for r in body["categories"]}
    assert Decimal(body["executed_expense"]) == BULK_EXECUTED


# ── F-D — grouped by category_id, never by name ────────────────────────────


async def _seed_same_name_org(factory) -> dict:
    """A master and its subcategory carrying the **same** name, both with
    direct rows. ``categories`` has no UNIQUE(org_id, name) — this shape is
    reachable today, and it is exactly the legend ambiguity TBD-326 owns.
    """
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Same Name Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        master = Category(org_id=org.id, name="Home", slug="home",
                          type=CategoryType.EXPENSE)
        db.add(master)
        await db.flush()
        sub = Category(org_id=org.id, name="Home", slug="home-sub",
                       type=CategoryType.EXPENSE, parent_id=master.id)
        db.add(sub)
        await db.flush()
        db.add_all([
            _tx(org.id, acct.id, master.id, "master-a", "10.00", day=3),
            _tx(org.id, acct.id, master.id, "master-b", "1.00", day=4),
            _tx(org.id, acct.id, sub.id, "sub-a", "22.00", day=5),
        ])
        await db.commit()
        return {
            "org_id": org.id, "user_id": user.id,
            "master_id": master.id, "sub_id": sub.id,
        }


@pytest.mark.asyncio
async def test_fd_master_and_sub_are_separate_rows_keyed_by_id(session_factory):
    """F-D. A master carrying DIRECT rows (11.00) and a subcategory carrying
    its own (22.00) — **both named "Home"**.

    Each must appear as its own row keyed by its own ``category_id``, with the
    sub carrying ``parent_id`` = the master's id.

    Mutant killed: grouping by ``category_name``. The identical names are what
    makes that mutant observable: under it the two collapse into ONE row of
    33.00, and with two DIFFERENT names it would have produced two rows and
    passed. A drilldown then opens a slice whose id it cannot name.
    """
    seed = await _seed_same_name_org(session_factory)
    app = _app(session_factory, seed["user_id"])
    with TestClient(app) as client:
        body = client.get(f"{SPEND_URL}?period_start={P_START}").json()

    rows = {r["category_id"]: r for r in body["categories"]}
    assert len(body["categories"]) == 2, body["categories"]
    assert set(rows) == {seed["master_id"], seed["sub_id"]}
    assert Decimal(rows[seed["master_id"]]["executed"]) == Decimal("11.00")
    assert Decimal(rows[seed["sub_id"]]["executed"]) == Decimal("22.00")
    assert rows[seed["master_id"]]["parent_id"] is None
    assert rows[seed["sub_id"]]["parent_id"] == seed["master_id"]
    # Both really do carry the same label — the fixture's whole point.
    assert rows[seed["master_id"]]["category_name"] == "Home"
    assert rows[seed["sub_id"]]["category_name"] == "Home"
    # No merged row exists under any key.
    assert all(
        Decimal(r["executed"]) != Decimal("33.00") for r in body["categories"]
    )
    assert Decimal(body["executed_expense"]) == Decimal("33.00")


# ── F-E — the four excluded row kinds, one category each ───────────────────
#
# One kind per category so a failure NAMES the kind. A single mixed category
# would go red as a lump, and `is_manual_adjustment` — the only one of the four
# already on the wire — is exactly the half-fix a lumped fence lets through.
#
# The totals below (3 + 4 + 5 + 6 = 18.00) exclude the Payroll income row by
# construction; if the type filter is lost the total becomes 918.00.


async def _seed_exclusions_org(factory) -> dict:
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Exclusions Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        adj = Category(org_id=org.id, name="Adjusted", slug="adjusted",
                       type=CategoryType.EXPENSE)
        rej = Category(org_id=org.id, name="Rejected", slug="rejected",
                       type=CategoryType.EXPENSE)
        out = Category(org_id=org.id, name="LegOut", slug="leg-out",
                       type=CategoryType.EXPENSE)
        inn = Category(org_id=org.id, name="LegIn", slug="leg-in",
                       type=CategoryType.EXPENSE)
        # ⚠ Added after the incoming-leg fence was MEASURED over-determined.
        # The transfer's income leg is excluded by the link filter AND by the
        # EXPENSE type filter, so a mutant that dropped the type filter left
        # every F-E test green — kind 4 was restating kind 3. This category
        # carries a plain settled INCOME row that no other predicate touches,
        # which makes the type filter attributable inside F-E instead of only
        # via F-B / F-C / F-F.
        pay = Category(org_id=org.id, name="Payroll", slug="payroll",
                       type=CategoryType.INCOME)
        db.add_all([adj, rej, out, inn, pay])
        await db.flush()

        # One ORDINARY row per category, each a distinct amount, so every
        # assertion below lands on a number no other branch produces.
        db.add_all([
            _tx(org.id, acct.id, adj.id, "adj ordinary", "3.00", day=2),
            _tx(org.id, acct.id, rej.id, "rej ordinary", "4.00", day=2),
            _tx(org.id, acct.id, out.id, "out ordinary", "5.00", day=2),
            _tx(org.id, acct.id, inn.id, "in ordinary", "6.00", day=2),
            # Kind 5 — ordinary settled INCOME. Not an "excluded row kind" in
            # the reportable-filter sense; excluded by TYPE, and fenced here
            # because nothing else in F-E could see that predicate.
            _tx(org.id, acct.id, pay.id, "payday", "900.00", day=3,
                tx_type=TransactionType.INCOME),
            # Kind 1 — manual balance adjustment.
            _tx(org.id, acct.id, adj.id, "adjustment", "700.00", day=6,
                is_manual_adjustment=True),
            # Kind 2 — a REJECTED reconciliation row. "Reverted, excluded,
            # retained for audit" — and the state a delete-demoted match
            # orphan lands in (CLAUDE.md's delete-demotion rule).
            _tx(org.id, acct.id, rej.id, "rejected", "1100.00", day=7,
                reconciliation_state="rejected"),
        ])
        await db.flush()

        # Kinds 3 and 4 — BOTH legs of one MUTUAL transfer, on two different
        # categories so each leg is attributable on its own. `_link_pair`
        # writes the link BIDIRECTIONALLY; that is what makes this a transfer
        # rather than a reconcile match.
        leg_out = _tx(org.id, acct.id, out.id, "leg out", "1300.00", day=8)
        leg_in = _tx(org.id, acct.id, inn.id, "leg in", "1300.00", day=8,
                     tx_type=TransactionType.INCOME)
        db.add_all([leg_out, leg_in])
        await db.flush()
        leg_out.linked_transaction_id = leg_in.id
        leg_in.linked_transaction_id = leg_out.id
        await db.commit()
        return {
            "org_id": org.id, "user_id": user.id,
            "adj_id": adj.id, "rej_id": rej.id,
            "out_id": out.id, "in_id": inn.id, "pay_id": pay.id,
        }


@pytest_asyncio.fixture
async def exclusions(session_factory):
    seed = await _seed_exclusions_org(session_factory)
    app = _app(session_factory, seed["user_id"])
    with TestClient(app) as c:
        res = c.get(f"{SPEND_URL}?period_start={P_START}")
        assert res.status_code == 200, res.text
        yield res.json(), seed


def test_fe_excludes_a_manual_balance_adjustment(exclusions):
    """F-E kind 1. ``is_manual_adjustment=True``, 700.00, on "Adjusted".

    This one is the BAIT: ``is_manual_adjustment`` is already on the wire
    (``schemas/transaction.py``), so it is the half of D1 a client-side or
    partial fix reaches first — and a fence covering only it stays green
    against an implementation that still counts REJECTED rows. It is fenced
    here as one of four, never alone.
    """
    body, seed = exclusions
    row = {r["category_id"]: r for r in body["categories"]}[seed["adj_id"]]
    assert Decimal(row["executed"]) == Decimal("3.00")


def test_fe_excludes_a_rejected_reconciliation_row(exclusions):
    """F-E kind 2. ``reconciliation_state="rejected"``, 1100.00, on "Rejected".

    Its amount was reverted from ``accounts.balance`` at the state transition;
    counting it here would put money in the donut that is in no balance.
    ``reconciliation_state`` is NOT on the wire, which is why this kind is the
    one a half-fix misses.
    """
    body, seed = exclusions
    row = {r["category_id"]: r for r in body["categories"]}[seed["rej_id"]]
    assert Decimal(row["executed"]) == Decimal("4.00")


def test_fe_excludes_the_outgoing_leg_of_a_mutual_transfer(exclusions):
    """F-E kind 3. The EXPENSE leg of a reciprocal pair, 1300.00, on "LegOut".

    A transfer moves money between the org's own accounts; it is not spending.
    This is the leg that would otherwise land in an expense rollup.
    """
    body, seed = exclusions
    row = {r["category_id"]: r for r in body["categories"]}[seed["out_id"]]
    assert Decimal(row["executed"]) == Decimal("5.00")


def test_fe_excludes_the_incoming_leg_of_a_mutual_transfer(exclusions):
    """F-E kind 4. The INCOME leg of the same reciprocal pair, 1300.00, on
    "LegIn".

    ⚠ **Over-determined, and said plainly rather than left implied.** This leg
    is excluded by TWO independent predicates — the link filter and the
    ``EXPENSE``-only type filter — so NO single-predicate mutant admits it, and
    this test cannot distinguish which predicate did the work. Measured: it
    stays green under the half-fix filter AND under a dropped type filter. It
    is kept because "the payload must not contain either leg" is the property
    the ticket owes, and dropped as a claim to fence the link filter — kind 3
    is what does that, and ``test_fe_excludes_settled_income`` is what covers
    the type filter.
    """
    body, seed = exclusions
    rows = {r["category_id"]: r for r in body["categories"]}
    assert Decimal(rows[seed["in_id"]]["executed"]) == Decimal("6.00")
    assert all(
        Decimal(r["executed"]) != Decimal("1300.00") for r in body["categories"]
    )


def test_fe_excludes_settled_income(exclusions):
    """F-E kind 5. A plain SETTLED INCOME row, 900.00, on "Payroll".

    Not one of the reportable-filter exclusions — this one fences the
    ``TransactionType.EXPENSE`` predicate, which the other four cannot see.
    Its category must be absent from the payload entirely: an income row that
    leaked into a spending rollup would inflate the donut by the org's whole
    salary, and the donut is where the user reads "what did I spend".
    """
    body, seed = exclusions
    assert seed["pay_id"] not in {r["category_id"] for r in body["categories"]}
    assert "Payroll" not in {r["category_name"] for r in body["categories"]}


def test_fe_total_is_the_ordinary_rows_only(exclusions):
    """F-E roll-up. 3 + 4 + 5 + 6 = 18.00 and nothing else. A single number
    that goes red if ANY of the five kinds re-enters, including via a total
    computed by a second query that skipped the filter.
    """
    body, _ = exclusions
    assert Decimal(body["executed_expense"]) == Decimal("18.00")
    assert sum(Decimal(r["executed"]) for r in body["categories"]) == Decimal(
        "18.00"
    )


# ── F-F — control: no expense is an empty rollup, not an error ─────────────


@pytest.mark.asyncio
async def test_ff_org_with_no_expense_returns_empty_rollup(session_factory):
    """F-F. An org with a CLOSED January period, one settled INCOME row and no
    expense whatsoever → **200**, ``categories: []``, ``executed_expense: "0"``.

    The income row is deliberate: it proves the emptiness comes from the
    ``EXPENSE`` type filter and not from a missing period or a missing org,
    either of which would make this control pass for the wrong reason. The
    donut's own empty state depends on this being a 200 — a 404 or a 500 here
    is what the ticket is removing, so answering one is not an improvement.
    """
    async with session_factory() as db:
        org, user, acct = await _base_org(db, name="Income Only Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))
        salary = Category(org_id=org.id, name="Salary", slug="salary",
                          type=CategoryType.INCOME)
        db.add(salary)
        await db.flush()
        db.add(_tx(org.id, acct.id, salary.id, "pay", "4200.00", day=25,
                   tx_type=TransactionType.INCOME))
        await db.commit()
        user_id = user.id

    app = _app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.get(f"{SPEND_URL}?period_start={P_START}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["categories"] == []
    assert body["executed_expense"] == "0"
    assert body["period_start"] == P_START.isoformat()
    assert body["period_end"] == P_END.isoformat()


# ── F-G — the tenant boundary, fenced with a decoy org that HAS money ──────
#
# ⚠ Every seeder above calls ``_base_org`` exactly ONCE, so every fence above
# runs against a single organization whose id is 1. Under a one-tenant fixture
# the correct implementation and a leaking one AGREE — there is nothing to
# leak. Measured: delete ``Transaction.org_id == org_id`` from
# ``executed_expense_by_category``, or ``Category.org_id == org_id`` from
# ``load_category_meta``, and all fifteen stay green. Nor was the boundary
# fenced elsewhere for this code path: ``tests/services/
# test_forecast_overdue_recurring.py``'s ``_skew_ids`` seeds a decoy org with
# accounts and categories only — NO transactions — so it skews ids without
# ever putting another tenant's money inside the window.
#
# This section is the fixture that makes the two implementations disagree.

DECOY_NAME = "DECOY-ORG-B-SECRET-CATEGORY"
DECOY_EXECUTED = Decimal("999.00")
SUBJECT_EXECUTED = Decimal("19.00")  # 12.00 on its own category + 7.00 stale


async def _seed_tenant_boundary_orgs(factory) -> dict:
    """TWO orgs, each with its own CLOSED January period and its own settled
    reportable EXPENSE inside it.

    Org A (the subject) holds 12.00 on its own ``Groceries`` category **plus**
    one 7.00 row whose ``category_id`` points at a category owned by Org B.
    That cross-tenant id is not decoration: it is the only shape under which
    ``load_category_meta``'s ``Category.org_id`` predicate is observable from
    the endpoint at all, because an org-scoped rollup otherwise never produces
    a foreign id to look up — remove the predicate and nothing changes. The
    shape is reachable because ``transactions.category_id`` is a plain FK to
    ``categories.id`` with no org predicate behind it, and it is precisely the
    "stale id from another tenant" ``load_category_meta``'s docstring promises
    resolves to nothing.

    Org B (the decoy) holds 999.00 under a category named
    ``DECOY-ORG-B-SECRET-CATEGORY``, and that category has a PARENT — so a name
    leak shows up in ``parent_id`` as well as in ``category_name``, and the
    failure output names the leak instead of reporting a bare mismatch. Its
    999.00 dwarfs Org A's 19.00, so a rollup that lost its org filter moves the
    total by two orders of magnitude rather than by a rounding-sized amount.
    """
    async with factory() as db:
        org_a, user_a, acct_a = await _base_org(db, name="Subject Org A")
        org_b, user_b, acct_b = await _base_org(
            db, name="Decoy Org B", username="decoy-owner"
        )
        db.add_all([
            BillingPeriod(org_id=org_a.id, start_date=P_START, end_date=P_END),
            BillingPeriod(org_id=org_b.id, start_date=P_START, end_date=P_END),
        ])
        groceries = Category(org_id=org_a.id, name="Groceries", slug="groceries",
                             type=CategoryType.EXPENSE)
        decoy_parent = Category(org_id=org_b.id, name="DECOY-PARENT",
                                slug="decoy-parent", type=CategoryType.EXPENSE)
        db.add_all([groceries, decoy_parent])
        await db.flush()
        decoy = Category(org_id=org_b.id, name=DECOY_NAME, slug="decoy",
                         type=CategoryType.EXPENSE, parent_id=decoy_parent.id)
        db.add(decoy)
        await db.flush()

        db.add_all([
            _tx(org_a.id, acct_a.id, groceries.id, "a-groceries", "12.00", day=4),
            _tx(org_a.id, acct_a.id, decoy.id, "a-stale-id", "7.00", day=6),
            _tx(org_b.id, acct_b.id, decoy.id, "b-secret", "999.00", day=8),
        ])
        await db.commit()
        return {
            "org_a": org_a.id, "user_a": user_a.id,
            "org_b": org_b.id, "user_b": user_b.id,
            "groceries_id": groceries.id,
            "decoy_id": decoy.id, "decoy_parent_id": decoy_parent.id,
        }


@pytest.mark.asyncio
async def test_fg_rollup_excludes_another_orgs_settled_expense(session_factory):
    """F-G, rollup half. Org B holds 999.00 of settled reportable EXPENSE in
    the SAME January window as Org A. Org A's payload must total 19.00.

    Mutant killed: dropping ``Transaction.org_id == org_id`` from
    ``executed_expense_by_category``. Under it Org A's total becomes 1018.00
    and another tenant's 999.00 lands in Org A's donut.
    """
    seed = await _seed_tenant_boundary_orgs(session_factory)
    app = _app(session_factory, seed["user_a"])
    with TestClient(app) as client:
        res = client.get(f"{SPEND_URL}?period_start={P_START}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["executed_expense"]) == SUBJECT_EXECUTED
    rows = {r["category_id"]: r for r in body["categories"]}
    assert set(rows) == {seed["groceries_id"], seed["decoy_id"]}
    assert Decimal(rows[seed["groceries_id"]]["executed"]) == Decimal("12.00")
    # The stale-id row is Org A's OWN money and stays; only its NAME is denied.
    assert Decimal(rows[seed["decoy_id"]]["executed"]) == Decimal("7.00")
    assert sum(
        Decimal(r["executed"]) for r in body["categories"]
    ) == SUBJECT_EXECUTED


@pytest.mark.asyncio
async def test_fg_category_name_lookup_is_org_scoped(session_factory):
    """F-G, name half. Org A carries one row whose ``category_id`` belongs to
    Org B. Its slice must render ``"Unknown"`` with ``parent_id: null`` — the
    behaviour ``load_category_meta``'s docstring claims ("a stale id from
    another tenant resolves to nothing rather than leaking a name") and that
    nothing tested.

    Mutant killed: dropping ``Category.org_id == org_id`` from
    ``load_category_meta``. Under it the slice renders
    ``DECOY-ORG-B-SECRET-CATEGORY`` and carries Org B's parent id.

    ⚠ That mutant is invisible to the rollup fence above and to all fifteen
    pre-existing tests: it changes no number, only a label — which is exactly
    what a totals-only fence cannot see.
    """
    seed = await _seed_tenant_boundary_orgs(session_factory)
    app = _app(session_factory, seed["user_a"])
    with TestClient(app) as client:
        body = client.get(f"{SPEND_URL}?period_start={P_START}").json()

    rows = {r["category_id"]: r for r in body["categories"]}
    assert rows[seed["decoy_id"]]["category_name"] == "Unknown"
    assert rows[seed["decoy_id"]]["parent_id"] is None
    names = {r["category_name"] for r in body["categories"]}
    assert DECOY_NAME not in names, names
    assert "DECOY-PARENT" not in names, names


@pytest.mark.asyncio
async def test_fg_control_each_org_reads_its_own_rollup(session_factory):
    """F-G control, and it is load-bearing twice.

    Read as **Org B**, the same fixture answers 999.00 under the decoy's real
    name and real ``parent_id``. So:

    * a rollup that answered every caller with Org A's payload would satisfy
      both fences above, and fails here; and
    * more importantly, the ``"Unknown"`` asserted above is evidence of the ORG
      SCOPE rather than of a name lookup that resolves nothing for anybody — a
      ``load_category_meta`` mutated to ``return {}`` passes the name fence and
      dies here.
    """
    seed = await _seed_tenant_boundary_orgs(session_factory)
    app = _app(session_factory, seed["user_b"])
    with TestClient(app) as client:
        body = client.get(f"{SPEND_URL}?period_start={P_START}").json()

    rows = {r["category_id"]: r for r in body["categories"]}
    assert set(rows) == {seed["decoy_id"]}
    assert Decimal(body["executed_expense"]) == DECOY_EXECUTED
    assert rows[seed["decoy_id"]]["category_name"] == DECOY_NAME
    assert rows[seed["decoy_id"]]["parent_id"] == seed["decoy_parent_id"]


# ── F-H — the route requires authentication ────────────────────────────────
#
# ⚠ Every fence above builds its app through ``_app``, which passes
# ``current_user=`` and therefore OVERRIDES ``get_current_user`` outright. None
# of them can see the route's auth dependency at all: delete
# ``Depends(get_current_user)`` from the handler and every one stays green.
# The two below build the app WITHOUT that override, so the real dependency
# runs.
#
# Scope: there is no route-auth inventory guard anywhere in ``backend/tests/``.
# That is a repo-wide gap and a separate ticket; these fence THIS route.


def _anonymous_app(factory) -> FastAPI:
    """The transactions router with ``get_current_user`` NOT overridden."""
    return make_test_app(factory, routers=[transactions_router])


@pytest.mark.asyncio
async def test_fh_anonymous_request_is_rejected(session_factory):
    """F-H. No ``Authorization`` header → **403 "Not authenticated"**, raised
    by ``HTTPBearer``'s ``auto_error`` before the handler body runs.

    The org is seeded with real settled expense on purpose: under the mutant
    there is something to leak, so the failure is a 200 carrying one org's
    spending to an unauthenticated caller rather than an empty 200.

    Mutant killed: removing ``current_user: User = Depends(get_current_user)``
    from the handler (resolving the org id some other way). That is a one-line
    edit which every other test in this file tolerates, because they all
    override the very dependency it deletes.
    """
    await _seed_gate_org(session_factory)
    app = _anonymous_app(session_factory)
    with TestClient(app) as client:
        res = client.get(f"{SPEND_URL}?period_start={P_START}")
        # Auth evaluates BEFORE query parsing: a malformed ``period_start``
        # from an anonymous caller is still 403, never 422 — so this route
        # exposes no pre-auth parsing surface.
        malformed = client.get(f"{SPEND_URL}?period_start=not-a-date")

    assert res.status_code == 403, res.text
    assert res.json()["detail"] == "Not authenticated"
    assert malformed.status_code == 403, malformed.text


@pytest.mark.asyncio
async def test_fh_undecodable_bearer_is_rejected(session_factory):
    """F-H, second half. A well-formed ``Authorization: Bearer`` header whose
    token does not decode → **401**, not 403 and not 200.

    The pair matters: 403-alone would also be satisfied by a bare
    ``HTTPBearer`` dependency that never resolves a user. The 401 comes from
    ``get_current_user``'s own ``decode_token`` arm, so together they pin that
    THIS dependency — not merely SOME security scheme — guards the route.
    """
    await _seed_gate_org(session_factory)
    app = _anonymous_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            f"{SPEND_URL}?period_start={P_START}",
            headers={"Authorization": "Bearer not-a-real-token"},
        )

    assert res.status_code == 401, res.text


# ── F-I — the substituted period, and the write this GET performs ──────────
#
# ⚠ **This GET can write, and can commit.** ``resolve_spend_window`` falls back
# to ``get_current_period`` when ``period_start`` matches no ``BillingPeriod``
# row, and ``get_current_period`` AUTO-CREATES and ``await db.commit()``s a
# period row for an org that has no open one (``billing_service.py``, TBD-297).
# Measured: an org goes 0 → 1 period rows from one GET. The behaviour is
# pre-existing — it moved verbatim out of ``compute_forecast`` — but it is
# newly reachable on an UNGATED route by an org that has Forecast switched
# off, so it is recorded here rather than left to be rediscovered.
#
# ⚠ What is fenced below is IDEMPOTENCE, deliberately, and NOT the write
# itself. A fence asserting "a GET creates a period row" would bless
# write-on-GET as contract and make removing it look like a regression;
# whether this GET should write at all is filed separately. The hazard that is
# a defect either way is DUPLICATE period creation, and that is what these pin.


@pytest.mark.asyncio
async def test_fi_unknown_period_start_is_substituted_not_404(session_factory):
    """F-I. A syntactically valid ``period_start`` matching NO
    ``BillingPeriod`` row is **silently substituted** with the org's current
    period: 200, carrying a ``period_start`` the caller did not send. It is
    not a 404 and not a 422.

    That is why the endpoint's docstring tells a caller to read
    ``period_start`` back off the response rather than trust what it sent. The
    org's period count must be unchanged — the substitution RESOLVES an
    existing period, it does not manufacture one for the requested date.

    The fixture's period is OPEN with nothing later on the roster, so
    ``period_spend_window_end`` returns ``None`` and ``resolve_spend_window``
    takes its ``derived is None`` tail fallback — the third of its three
    window shapes, and one no other test in this file reaches.

    Mutants killed: swapping the hand-rolled lookup for
    ``billing_service.resolve_period``, which RAISES on no-match so this
    becomes a 4xx/500; and a fallback that CREATES the requested period
    instead of resolving the current one, which makes the count 2.
    """
    async with session_factory() as db:
        org, user, acct = await _base_org(db, name="Open Period Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=None))
        food = Category(org_id=org.id, name="Food", slug="food",
                        type=CategoryType.EXPENSE)
        db.add(food)
        await db.flush()
        db.add(_tx(org.id, acct.id, food.id, "groceries", "8.00", day=5))
        await db.commit()
        org_id, user_id = org.id, user.id

    app = _app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.get(f"{SPEND_URL}?period_start=2019-03-07")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["period_start"] != "2019-03-07"
    assert body["period_start"] == P_START.isoformat()
    # ``p_start + 1 month - 1 day``: the tail fallback, since the open row has
    # no successor to derive an end from. No clock enters this arm at all.
    assert body["period_end"] == date(2026, 1, 31).isoformat()
    assert Decimal(body["executed_expense"]) == Decimal("8.00")

    async with session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(BillingPeriod)
            .where(BillingPeriod.org_id == org_id)
        )
    assert count == 1, f"the GET manufactured {count} period rows"


@pytest.mark.asyncio
async def test_fi_two_gets_leave_exactly_one_period_row(session_factory):
    """F-I. An org with **no** ``BillingPeriod`` rows at all, hit TWICE.

    Exactly ONE period row exists afterwards and the two responses are
    identical. This pins the real hazard — a GET that manufactures a fresh
    period on every call — without asserting that the GET creates one, which
    would bless write-on-GET as contract.

    ⚠ **Said plainly rather than left implied: this is a REGRESSION fence, and
    a single-line mutant of ``get_current_period`` does not redden it.** The
    idempotence is guarded in depth — the open-row re-select, the
    ``uq_billing_period_org_start`` unique constraint, and the
    ``IntegrityError`` arm that recovers by re-reading — so removing any ONE of
    the three leaves the count at 1. Three were measured surviving. What DOES
    redden it is a ``resolve_spend_window`` whose fallback inlines the period
    creation instead of delegating to ``get_current_period``: the second call
    then hits the unique constraint with nothing to catch it. That is the shape
    a future "why is there a fourth copy of this SELECT" edit produces, and it
    is the named mutant this test exists for. Do not read the green as proof
    that idempotence is pinned from every direction.

    ⚠ Clock: nothing here is asserted against a calendar literal. The only
    clock sensitivity is a billing-cycle rollover landing strictly between the
    two requests — midnight on the 1st, for ``billing_cycle_day=1``.
    """
    async with session_factory() as db:
        org, user, _acct = await _base_org(db, name="No Period Org")
        await db.commit()
        org_id, user_id = org.id, user.id

    app = _app(session_factory, user_id)
    with TestClient(app) as client:
        first = client.get(SPEND_URL)
        second = client.get(SPEND_URL)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    async with session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(BillingPeriod)
            .where(BillingPeriod.org_id == org_id)
        )
    assert count == 1, f"two GETs left {count} period rows"


# ── F-J — the OPEN-period arm, and an omitted period_start ─────────────────
#
# ⚠ Every fixture above creates a CLOSED period, so ``resolve_spend_window``
# always took ``window_end = period.end_date``. The OPEN arm —
# ``period_spend_window_end`` and its ``max(derived, today)`` floor — reached
# this endpoint through no test at all; it was exercised only through
# ``compute_forecast`` in F-C. The live dashboard always renders an OPEN
# period, so the path the product actually uses was the untested one, and
# ``SpendingByCategoryResponse``'s claim that ``period_end`` "for an open
# period is floored at today" was unfenced on this route. Likewise every test
# above sends ``?period_start=``, so the ``None`` → ``get_current_period``
# branch was never taken from the wire.
#
# ⚠ Dates below are anchored to ``date.today()``, never to calendar literals
# (``reference_wall_clock_date_bomb_tests``). The 2026 literals above are safe
# only because they sit on CLOSED periods whose window reads no clock; an open
# period has no ``today=`` injection point through HTTP, so here the floor IS
# the clock and the fixture has to move with it.


def _tx_on(org_id, acct_id, cat_id, desc, amount, *, on: date,
           tx_type=TransactionType.EXPENSE,
           status=TransactionStatus.SETTLED, **kw) -> Transaction:
    """``_tx`` for a date computed relative to ``date.today()``.

    ``_tx`` places rows inside a fixed 2026 calendar via ``day=``/``month=``,
    which is right for the CLOSED-period fixtures above and unusable for the
    open-period ones below.
    """
    return Transaction(
        org_id=org_id, account_id=acct_id, category_id=cat_id,
        description=desc, amount=Decimal(amount), type=tx_type, status=status,
        date=on,
        settled_date=on if status is TransactionStatus.SETTLED else None,
        **kw,
    )


@pytest.mark.asyncio
async def test_fj_open_period_end_is_floored_at_today(session_factory):
    """F-J. An OPEN period starting 40 days ago with a later period starting
    20 days ago on the roster, so ``period_effective_end`` derives
    ``today - 21d`` and the floor lifts the window end to **today**.

    Pinned from both sides:

    * 12.00 dated ``today - 30d`` — inside the derived window AND the floored
      one, so a wholly broken window is distinguishable from a lost floor;
    * 33.00 dated ``today - 5d`` — inside the FLOORED window only;
    * 77.00 dated ``today + 5d`` — outside either, so the floor is shown to
      stop AT today rather than to have been removed.

    Mutant killed: ``resolve_spend_window``'s open arm calling
    ``period_effective_end`` instead of ``period_spend_window_end`` — i.e.
    losing the floor. Under it ``period_end`` is ``today - 21d``, the total is
    12.00, and a settled expense the user can see in their transaction list is
    missing from the tile that tells them what they spent.

    ``period_end`` is asserted against the clock read AROUND the request rather
    than against a single ``date.today()``, so a midnight rollover mid-test
    cannot turn this red without there also being a real failure.
    """
    today = date.today()
    p_start = today - timedelta(days=40)
    next_start = today - timedelta(days=20)
    derived_end = next_start - timedelta(days=1)

    async with session_factory() as db:
        org, user, acct = await _base_org(db, name="Open Floored Org")
        db.add_all([
            BillingPeriod(org_id=org.id, start_date=p_start, end_date=None),
            BillingPeriod(org_id=org.id, start_date=next_start, end_date=None),
        ])
        food = Category(org_id=org.id, name="Food", slug="food",
                        type=CategoryType.EXPENSE)
        db.add(food)
        await db.flush()
        db.add_all([
            _tx_on(org.id, acct.id, food.id, "inside-derived", "12.00",
                   on=today - timedelta(days=30)),
            _tx_on(org.id, acct.id, food.id, "inside-floor-only", "33.00",
                   on=today - timedelta(days=5)),
            _tx_on(org.id, acct.id, food.id, "beyond-today", "77.00",
                   on=today + timedelta(days=5)),
        ])
        await db.commit()
        user_id = user.id

    app = _app(session_factory, user_id)
    before = date.today()
    with TestClient(app) as client:
        res = client.get(f"{SPEND_URL}?period_start={p_start}")
    after = date.today()

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["period_start"] == p_start.isoformat()
    assert body["period_end"] in {before.isoformat(), after.isoformat()}
    # Stated absolutely too: the UNFLOORED value must not be what came back.
    assert body["period_end"] != derived_end.isoformat()
    assert Decimal(body["executed_expense"]) == Decimal("45.00")


@pytest.mark.asyncio
async def test_fj_omitted_period_start_resolves_the_current_period(
    session_factory,
):
    """F-J, second half. ``GET`` with **no** ``period_start`` at all — the
    ``None`` → ``get_current_period`` branch, which every test above skips
    because every one of them sends ``?period_start=``.

    The org holds a CLOSED period 70..41 days ago carrying 555.00 and an OPEN
    period starting 10 days ago carrying 42.00. The omitted-argument response
    must be the OPEN one: ``period_start`` 10 days ago, 42.00, and no trace of
    the 555.00 anywhere in the payload.

    Mutant killed: ``get_current_period``'s open-row select flipped to
    ``BillingPeriod.end_date.is_not(None)``. Under it the omitted argument
    resolves to the CLOSED period and the dashboard's default view reports
    two-month-old spend as "this period".
    """
    today = date.today()
    open_start = today - timedelta(days=10)
    closed_start = today - timedelta(days=70)
    closed_end = today - timedelta(days=41)

    async with session_factory() as db:
        org, user, acct = await _base_org(db, name="Default Period Org")
        db.add_all([
            BillingPeriod(org_id=org.id, start_date=closed_start,
                          end_date=closed_end),
            BillingPeriod(org_id=org.id, start_date=open_start, end_date=None),
        ])
        old = Category(org_id=org.id, name="Old", slug="old",
                       type=CategoryType.EXPENSE)
        new = Category(org_id=org.id, name="New", slug="new",
                       type=CategoryType.EXPENSE)
        db.add_all([old, new])
        await db.flush()
        db.add_all([
            _tx_on(org.id, acct.id, old.id, "old spend", "555.00",
                   on=closed_start + timedelta(days=5)),
            _tx_on(org.id, acct.id, new.id, "new spend", "42.00",
                   on=today - timedelta(days=3)),
        ])
        await db.commit()
        user_id, new_id = user.id, new.id

    app = _app(session_factory, user_id)
    with TestClient(app) as client:
        res = client.get(SPEND_URL)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["period_start"] == open_start.isoformat()
    assert Decimal(body["executed_expense"]) == Decimal("42.00")
    rows = {r["category_id"]: r for r in body["categories"]}
    assert set(rows) == {new_id}
    assert "Old" not in {r["category_name"] for r in body["categories"]}


# ── F-C — compute_forecast stays byte-identical to main ────────────────────
#
# Two fixtures, one per arm of the window derivation this ticket MOVES out of
# `compute_forecast` and into the shared helper:
#
#   * CLOSED period  → `window_end = period.end_date`, verbatim, never floored.
#   * OPEN period    → `period_spend_window_end`, whose open-interior arm is
#                      `max(derived_end, today)`. The fixture is deliberately
#                      LAPSED (today is three weeks past the derived end) and
#                      carries a February settled row, so the snapshot moves if
#                      the floor is lost. That is the documented over-count
#                      residual, pinned here as behaviour rather than argued.
#
# Both call the SERVICE directly with an injected `today=`, so no wall clock
# enters and both snapshots pin the exact key ORDER of the response, not just
# its content.


async def _seed_forecast_snapshot_org(factory, *, closed: bool) -> int:
    """Executed + pending + recurring across four categories.

    ``Rentals`` carries a recurring template and NO transaction, so the
    category-name lookup must reach an id that the executed rollup never
    produced — the exact merge this ticket rewires.
    """
    async with factory() as db:
        org, user, acct = await _base_org(
            db, name="Snapshot Closed" if closed else "Snapshot Open"
        )
        db.add(BillingPeriod(
            org_id=org.id, start_date=P_START,
            end_date=P_END if closed else None,
        ))
        if not closed:
            # A NEXT period, so `period_effective_end` derives 2026-01-31 for
            # the open row and the floor has something to floor.
            db.add(BillingPeriod(
                org_id=org.id, start_date=date(2026, 2, 1), end_date=None,
            ))
        food = Category(org_id=org.id, name="Food", slug="food",
                        type=CategoryType.EXPENSE)
        util = Category(org_id=org.id, name="Utilities", slug="utilities",
                        type=CategoryType.EXPENSE)
        rent = Category(org_id=org.id, name="Rentals", slug="rentals",
                        type=CategoryType.EXPENSE)
        salary = Category(org_id=org.id, name="Salary", slug="salary",
                          type=CategoryType.INCOME)
        db.add_all([food, util, rent, salary])
        await db.flush()

        rows = [
            _tx(org.id, acct.id, food.id, "groceries", "120.50", day=5),
            _tx(org.id, acct.id, food.id, "dinner", "40.25", day=9),
            _tx(org.id, acct.id, salary.id, "pay", "3000.00", day=2,
                tx_type=TransactionType.INCOME),
            # PENDING with a settled-date ESTIMATE, so the effective-date
            # bucketing is in the snapshot too.
            _tx(org.id, acct.id, util.id, "power", "88.00", day=20,
                status=TransactionStatus.PENDING, settled_day=28),
            # Excluded kinds, so the snapshot also pins the filter.
            _tx(org.id, acct.id, food.id, "adjustment", "500.00", day=11,
                is_manual_adjustment=True),
            _tx(org.id, acct.id, util.id, "rejected", "900.00", day=12,
                reconciliation_state="rejected"),
        ]
        if not closed:
            # Inside the FLOORED window only. Drops out the moment the floor
            # is lost, which is what makes the open snapshot sensitive to it.
            rows.append(
                _tx(org.id, acct.id, food.id, "february", "17.00",
                    month=2, day=10)
            )
        db.add_all(rows)

        db.add(RecurringTransaction(
            org_id=org.id, account_id=acct.id, category_id=rent.id,
            description="rent", amount=Decimal("650.00"), type="expense",
            frequency=Frequency.MONTHLY, next_due_date=date(2026, 1, 15),
            is_active=True,
        ))
        await db.commit()
        return org.id


# ⚠ RECORDED FROM ``main``. Both literals below were produced by running this
# file's fixtures against ``main``'s ``forecast_service`` (no implementation
# change in the tree), then REPLAYED against it with the implementation stashed
# — see the module footnote. A snapshot recorded from this branch would be
# vacuously green.
SNAPSHOT_CLOSED = """\
{
  "period_start": "2026-01-01",
  "period_end": "2026-01-31",
  "executed_income": "3000.00",
  "executed_expense": "160.75",
  "executed_net": "2839.25",
  "pending_income": "0",
  "pending_expense": "88.00",
  "recurring_income": "0",
  "recurring_expense": "650.00",
  "forecast_income": "3000.00",
  "forecast_expense": "898.75",
  "forecast_net": "2101.25",
  "categories": [
    {
      "category_id": 1,
      "category_name": "Food",
      "parent_id": null,
      "executed": "160.75",
      "pending": "0",
      "recurring": "0",
      "forecast": "160.75"
    },
    {
      "category_id": 2,
      "category_name": "Utilities",
      "parent_id": null,
      "executed": "0",
      "pending": "88.00",
      "recurring": "0",
      "forecast": "88.00"
    },
    {
      "category_id": 3,
      "category_name": "Rentals",
      "parent_id": null,
      "executed": "0",
      "pending": "0",
      "recurring": "650.00",
      "forecast": "650.00"
    }
  ]
}"""

SNAPSHOT_OPEN = """\
{
  "period_start": "2026-01-01",
  "period_end": "2026-02-20",
  "executed_income": "3000.00",
  "executed_expense": "177.75",
  "executed_net": "2822.25",
  "pending_income": "0",
  "pending_expense": "88.00",
  "recurring_income": "0",
  "recurring_expense": "1300.00",
  "forecast_income": "3000.00",
  "forecast_expense": "1565.75",
  "forecast_net": "1434.25",
  "categories": [
    {
      "category_id": 1,
      "category_name": "Food",
      "parent_id": null,
      "executed": "177.75",
      "pending": "0",
      "recurring": "0",
      "forecast": "177.75"
    },
    {
      "category_id": 2,
      "category_name": "Utilities",
      "parent_id": null,
      "executed": "0",
      "pending": "88.00",
      "recurring": "0",
      "forecast": "88.00"
    },
    {
      "category_id": 3,
      "category_name": "Rentals",
      "parent_id": null,
      "executed": "0",
      "pending": "0",
      "recurring": "1300.00",
      "forecast": "1300.00"
    }
  ]
}"""


@pytest.mark.asyncio
async def test_fc_compute_forecast_byte_identical_closed_period(session_factory):
    """F-C, CLOSED arm. ``window_end = period.end_date``, taken verbatim.

    Compares ``json.dumps(fc, indent=2)`` — so the fence pins the exact key
    ORDER and the exact string serialisation of every Decimal, not merely a
    dict that happens to compare equal.
    """
    async with session_factory() as db:
        org_id = await _seed_forecast_snapshot_org(session_factory, closed=True)
        fc = await forecast_service.compute_forecast(
            db, org_id, period_start=P_START, today=date(2026, 1, 15)
        )
    assert json.dumps(fc, indent=2) == SNAPSHOT_CLOSED


@pytest.mark.asyncio
async def test_fc_compute_forecast_byte_identical_open_floored_period(
    session_factory,
):
    """F-C, OPEN arm. ``period_spend_window_end`` derives 2026-01-31 from the
    next period's start and then FLOORS at ``today=2026-02-20``, so the window
    is ``[2026-01-01, 2026-02-20]`` and the February settled row is inside it.

    Both halves are load-bearing: without the next period there is nothing to
    derive, and without the lapsed clock there is nothing to floor.
    """
    async with session_factory() as db:
        org_id = await _seed_forecast_snapshot_org(session_factory, closed=False)
        fc = await forecast_service.compute_forecast(
            db, org_id, period_start=P_START, today=date(2026, 2, 20)
        )
    assert json.dumps(fc, indent=2) == SNAPSHOT_OPEN


# ── footnote: how F-C was proven non-vacuous ───────────────────────────────
#
# Four green snapshot assertions alone would be equally consistent with the
# container never having loaded `main`'s source. So the replay was run BOTH
# ways and both halves are the evidence:
#
#   * the implementation files were stashed (this test file kept). The two F-C
#     snapshots PASSED — proving they really are `main`'s bytes — while every
#     other fence in this file went RED, because the endpoint does not exist on
#     `main`. The red half is what makes the green half evidence.
#   * with the implementation restored, all fences pass together.
