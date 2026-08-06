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

All dates are fixed literals and every clock-sensitive call is given an
explicit ``today=``; nothing here is ``date.today()``-relative
(``reference_wall_clock_date_bomb_tests``).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
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


async def _base_org(db, *, name: str) -> tuple[Organization, User, Account]:
    org = Organization(name=name, billing_cycle_day=1)
    db.add(org)
    await db.flush()
    user = User(
        org_id=org.id, username="owner", email=f"owner@{org.id}.example",
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
