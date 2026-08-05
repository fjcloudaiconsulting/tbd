"""TBD-221 PR A — the two additive query params on GET /api/v1/transactions.

Design note: ``specs/2026-08-05-spending-donut-server-rollup.md`` §4 and §7.

PR A adds ``reportable`` and ``category_match`` so the dashboard donut's
drilldown can reproduce the ``/api/v1/forecast`` per-category rollup's WHERE
clause exactly. Both defaults preserve today's behaviour byte-for-byte: this
endpoint is PAT-reachable external contract surface (TBD-268 §5), so an
additive change that moved a default would be a breaking change.

Fences, per §7:

* ``B1`` — GUARD, not a fence. ``sum(row.executed) == executed_expense`` on
  ``/api/v1/forecast`` already holds on unmodified ``main``:
  ``forecast_service.py:138-147`` (the scalar) and ``:266-278`` (the rollup)
  are the same predicate modulo the ``group_by``. It cannot go red against any
  PR-A implementation because PR A does not touch ``forecast_service``. It
  earns its place on the fixture, not on the assertion: it is the only test in
  the repo that runs that equality over a period holding MORE THAN 200 rows,
  which is the row count under which the client-side donut it replaces silently
  truncates. ``test_b1_fixture_exceeds_the_200_row_cap`` pins the fixture size
  so a later shrink cannot quietly delete the point.
* ``B2`` — FENCE. ``category_match=exact`` vs ``subtree``. Kills
  "``exact`` not wired, falls through to subtree".
* ``B3`` — FENCE. ``reportable=true`` drops BOTH a manual adjustment AND a
  REJECTED reconciliation row (and the transfer legs). ``is_manual_adjustment``
  alone is the half-fix: it is already on the wire, so a filter that handles
  only it looks correct and still counts rejected rows.
* ``B4`` — CONTROL. With both params absent the response bytes are the ones
  ``main`` emits for the same fixture. Green before AND after PR A by
  construction; it goes red only on a change that alters default behaviour.

All dates are fixed literals, never ``today``-relative. The list endpoint
never reads the clock, and ``compute_forecast`` does not either once its
period is CLOSED (``forecast_service.py:112`` takes ``period.end_date``
verbatim), so the whole file is deterministic.
"""
from __future__ import annotations

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
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.forecast import router as forecast_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from tests.factories import make_test_app


P_START = date(2026, 1, 1)
P_END = date(2026, 1, 31)

# §7 B1: the row count the client-side donut it replaces could not see past.
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


def _make_app(factory, user_id: int) -> FastAPI:
    async def _resolve(f) -> User:
        async with f() as db:
            return await db.get(User, user_id)

    return make_test_app(
        factory,
        routers=[transactions_router, forecast_router],
        current_user=_resolve,
    )


# ── the >200-row fixture (B1, B2, B3) ──────────────────────────────────────
#
# Counts are chosen so every assertion below is a distinct number: a filter
# that silently falls through to another branch cannot land on the right
# total by coincidence.
HOME_DIRECT = 90       # rows sitting DIRECTLY on the master category
UTILITIES_ROWS = 80    # rows on its ONLY subcategory
FOOD_ROWS = 80         # an unrelated master, no children


async def _seed_rollup_org(factory) -> dict:
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Rollup Org")
        db.add(BillingPeriod(org_id=org.id, start_date=P_START, end_date=P_END))

        home = Category(
            org_id=org.id, name="Home", slug="home", type=CategoryType.EXPENSE
        )
        food = Category(
            org_id=org.id, name="Food", slug="food", type=CategoryType.EXPENSE
        )
        misc = Category(
            org_id=org.id, name="Misc", slug="misc", type=CategoryType.EXPENSE
        )
        travel = Category(
            org_id=org.id, name="Travel", slug="travel", type=CategoryType.EXPENSE
        )
        salary = Category(
            org_id=org.id, name="Salary", slug="salary", type=CategoryType.INCOME
        )
        db.add_all([home, food, misc, travel, salary])
        await db.flush()
        utilities = Category(
            org_id=org.id, name="Utilities", slug="utilities",
            type=CategoryType.EXPENSE, parent_id=home.id,
        )
        db.add(utilities)
        await db.flush()

        def _row(cat_id, desc, amount, *, day, tx_type=TransactionType.EXPENSE,
                 status=TransactionStatus.SETTLED, **kw):
            d = date(2026, 1, day)
            return Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_id,
                description=desc, amount=Decimal(amount), type=tx_type,
                status=status, date=d,
                settled_date=d if status is TransactionStatus.SETTLED else None,
                **kw,
            )

        rows: list[Transaction] = []
        for i in range(HOME_DIRECT):
            rows.append(_row(home.id, f"home-{i}", "1.00", day=(i % 26) + 2))
        for i in range(UTILITIES_ROWS):
            rows.append(_row(utilities.id, f"util-{i}", "2.00", day=(i % 26) + 2))
        for i in range(FOOD_ROWS):
            rows.append(_row(food.id, f"food-{i}", "3.00", day=(i % 26) + 2))

        # B3's four non-reportable kinds, all on `misc` so the drilldown that
        # opens the Misc slice is the exact query under test.
        rows.append(_row(misc.id, "misc ordinary", "5.00", day=10))
        rows.append(_row(
            misc.id, "misc adjustment", "7.00", day=11, is_manual_adjustment=True,
        ))
        rows.append(_row(
            misc.id, "misc rejected", "11.00", day=12,
            reconciliation_state="rejected",
        ))

        # A category whose only row is PENDING: it appears in the rollup with
        # executed="0", so B1 stays an assertion about EXECUTED, not forecast.
        rows.append(_row(
            travel.id, "travel pending", "50.00", day=13,
            status=TransactionStatus.PENDING,
        ))
        # Income, so a rollup that dropped the type filter would break B1.
        rows.append(_row(salary.id, "pay-1", "100.00", day=5,
                         tx_type=TransactionType.INCOME))
        rows.append(_row(salary.id, "pay-2", "100.00", day=20,
                         tx_type=TransactionType.INCOME))

        db.add_all(rows)
        await db.flush()

        # A real (mutual) transfer pair on `misc`. reportable_transaction_filter
        # drops both legs; it is a strict superset of collapse_transfers, which
        # is why the drilldown must NOT send both (design note §3).
        leg_a = _row(misc.id, "misc leg out", "13.00", day=14)
        leg_b = _row(misc.id, "misc leg in", "13.00", day=14,
                     tx_type=TransactionType.INCOME)
        db.add_all([leg_a, leg_b])
        await db.flush()
        leg_a.linked_transaction_id = leg_b.id
        leg_b.linked_transaction_id = leg_a.id

        await db.commit()
        return {
            "user_id": user.id,
            "home_id": home.id,
            "utilities_id": utilities.id,
            "food_id": food.id,
            "misc_id": misc.id,
        }


@pytest_asyncio.fixture
async def rollup(session_factory):
    seed = await _seed_rollup_org(session_factory)
    app = _make_app(session_factory, seed["user_id"])
    with TestClient(app) as c:
        yield c, seed


# ── B1 — guard: the rollup and the scalar agree past 200 rows ──────────────


def test_b1_fixture_exceeds_the_200_row_cap(rollup):
    """The fixture IS the contribution. Pins it so a later shrink cannot
    silently turn B1 back into a duplicate of the existing sub-200 coverage."""
    client, _ = rollup
    res = client.get(
        f"/api/v1/transactions?date_from={P_START}&date_to={P_END}&limit=1"
    )
    assert res.status_code == 200
    assert res.json()["total"] > CAP


def test_b1_category_rollup_sums_to_executed_expense_past_200_rows(rollup):
    """GUARD (not a fence): holds on unmodified ``main`` and PR A does not
    touch ``forecast_service``. It is the DoD made executable at a row count
    no existing test reaches."""
    client, _ = rollup
    res = client.get(f"/api/v1/forecast?period_start={P_START}")
    assert res.status_code == 200
    body = res.json()
    assert body["period_start"] == P_START.isoformat()
    assert body["period_end"] == P_END.isoformat()

    rollup_total = sum(Decimal(row["executed"]) for row in body["categories"])
    assert rollup_total == Decimal(body["executed_expense"])
    # Nailed down absolutely too, so a rollup and a scalar that BOTH lost the
    # same rows would still fail: 90*1 + 80*2 + 80*3 + 5 (misc ordinary).
    assert Decimal(body["executed_expense"]) == Decimal("495.00")
    # The pending-only category is present with executed 0 — proof the sum is
    # over executed, not over forecast.
    travel = [c for c in body["categories"] if c["category_name"] == "Travel"]
    assert len(travel) == 1
    assert Decimal(travel[0]["executed"]) == Decimal("0")
    assert Decimal(travel[0]["pending"]) == Decimal("50.00")


# ── B2 — fence: category_match ─────────────────────────────────────────────


def test_b2_exact_returns_only_rows_directly_on_the_master(rollup):
    """FENCE. Kills ``exact`` not wired / falling through to subtree.

    THE LANDMINE (design note §4): ``category_id`` on this endpoint is
    master-includes-subs — a regression guard for the 2026-05-13 "the category
    filter did not filter anything" report — while the rollup PR B consumes
    groups by the row's OWN ``category_id``. Without ``exact`` a master slice's
    drilldown returns master PLUS every sub and sums to more than the slice it
    opened.
    """
    client, seed = rollup
    res = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}"
        f"&category_match=exact&limit=1"
    )
    assert res.status_code == 200
    assert res.json()["total"] == HOME_DIRECT

    body = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}"
        f"&category_match=exact&limit=200"
    ).json()
    assert {r["category_id"] for r in body["items"]} == {seed["home_id"]}


def test_b2_subtree_returns_the_master_and_its_subs(rollup):
    client, seed = rollup
    res = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}"
        f"&category_match=subtree&limit=1"
    )
    assert res.status_code == 200
    assert res.json()["total"] == HOME_DIRECT + UTILITIES_ROWS


def test_b2_omitting_the_param_is_subtree(rollup):
    """The default must stay master-includes-subs: the 2026-05-13 guard."""
    client, seed = rollup
    omitted = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}&limit=1"
    )
    explicit = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}"
        f"&category_match=subtree&limit=1"
    )
    assert omitted.status_code == 200
    assert omitted.json()["total"] == HOME_DIRECT + UTILITIES_ROWS
    assert omitted.json() == explicit.json()


def test_b2_exact_and_subtree_agree_on_a_leaf_category(rollup):
    """CONTROL for B2: a category with no children must read the same under
    both modes. Without this, an ``exact`` that simply ignored the subtree
    branch everywhere would still look like it "works"."""
    client, seed = rollup
    exact = client.get(
        f"/api/v1/transactions?category_id={seed['utilities_id']}"
        f"&category_match=exact&limit=1"
    ).json()
    subtree = client.get(
        f"/api/v1/transactions?category_id={seed['utilities_id']}"
        f"&category_match=subtree&limit=1"
    ).json()
    assert exact["total"] == UTILITIES_ROWS
    assert exact == subtree


def test_b2_rejects_an_unknown_match_mode(rollup):
    client, seed = rollup
    res = client.get(
        f"/api/v1/transactions?category_id={seed['home_id']}"
        f"&category_match=descendants"
    )
    assert res.status_code == 422


# ── B3 — fence: reportable ─────────────────────────────────────────────────


def _descriptions(client, query: str) -> set[str]:
    res = client.get(f"/api/v1/transactions?{query}&limit=200")
    assert res.status_code == 200
    return {r["description"] for r in res.json()["items"]}


def test_b3_default_returns_the_non_reportable_rows(rollup):
    """Baseline half of B3. Default ``reportable=false`` keeps everything —
    this is the behaviour every existing caller depends on."""
    client, seed = rollup
    assert _descriptions(
        client, f"category_id={seed['misc_id']}&category_match=exact"
    ) == {
        "misc ordinary", "misc adjustment", "misc rejected",
        "misc leg out", "misc leg in",
    }


def test_b3_reportable_excludes_adjustment_and_rejected_and_transfer_legs(rollup):
    """FENCE for D1. ⚠ BOTH non-reportable kinds are required.

    ``is_manual_adjustment`` is already on the wire (``schemas/transaction.py``),
    so a filter that handles only it passes a manual-adjustment-only fence while
    still returning REJECTED rows — the half-fix the design note names. The
    reciprocal transfer pair is asserted too because
    ``reportable_transaction_filter`` is a strict superset of
    ``collapse_transfers``.
    """
    client, seed = rollup
    got = _descriptions(
        client,
        f"category_id={seed['misc_id']}&category_match=exact&reportable=true",
    )
    assert got == {"misc ordinary"}


@pytest.mark.parametrize(
    "description",
    ["misc adjustment", "misc rejected", "misc leg out", "misc leg in"],
)
def test_b3_each_excluded_kind_individually(rollup, description):
    """Per-kind, so a fence that only ever asserted a COUNT cannot be satisfied
    by dropping the wrong row."""
    client, seed = rollup
    q = f"category_id={seed['misc_id']}&category_match=exact"
    assert description in _descriptions(client, q)
    assert description not in _descriptions(client, q + "&reportable=true")


def test_b3_reportable_total_matches_the_page(rollup):
    """``total`` is computed by a SECOND query. A ``reportable`` applied to the
    page query only would leave ``total`` counting the excluded rows — exactly
    the "list exceeds its own slice" defect PR B is deleting."""
    client, seed = rollup
    body = client.get(
        f"/api/v1/transactions?category_id={seed['misc_id']}"
        f"&category_match=exact&reportable=true&limit=200"
    ).json()
    assert body["total"] == len(body["items"]) == 1


def test_b3_reportable_composes_with_the_drilldown_query(rollup):
    """The exact URL shape §3 of the design note specifies, minus paging. Its
    ``total`` must equal the Misc slice of the B1 rollup, in rows AND amount."""
    client, seed = rollup
    body = client.get(
        f"/api/v1/transactions?category_id={seed['misc_id']}"
        f"&category_match=exact&reportable=true&type=expense&status=settled"
        f"&date_from={P_START}&date_to={P_END}&limit=200"
    ).json()
    slice_total = sum(Decimal(r["amount"]) for r in body["items"])

    forecast = client.get(f"/api/v1/forecast?period_start={P_START}").json()
    misc = [c for c in forecast["categories"] if c["category_id"] == seed["misc_id"]]
    assert len(misc) == 1
    assert slice_total == Decimal(misc[0]["executed"]) == Decimal("5.00")


# ── B4 — control: default response bytes are main's ────────────────────────
#
# Recorded on unmodified `main` (f32540a9) against `_seed_control_org` below.
# These are RESPONSE BYTES, not a re-derivation: a re-derived expectation moves
# with the implementation and cannot detect a changed default. If a legitimate
# contract change ever lands, re-record from `main` deliberately — do not patch
# a character.

CONTROL_SNAPSHOTS: dict[str, str] = {
    "category_id=1": (
        "{\"items\":[{\"id\":6,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\""
        "Home\",\"description\":\"leg in\",\"amount\":\"50.00\",\"type\":\"income\",\"status\":\"settled\",\"linked"
        "_transaction_id\":5,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"s"
        "ettled_date\":\"2026-01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\""
        "id\":5,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"descr"
        "iption\":\"leg out\",\"amount\":\"50.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transacti"
        "on_id\":6,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"settled_dat"
        "e\":\"2026-01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":4,\"acc"
        "ount_id\":1,\"account_name\":\"Acct\",\"category_id\":2,\"category_name\":\"Utilities\",\"descriptio"
        "n\":\"sub row\",\"amount\":\"40.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id"
        "\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-08\",\"settled_date\""
        ":\"2026-01-08\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":3,\"accou"
        "nt_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"rej"
        "ected\",\"amount\":\"30.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null"
        ",\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-07\",\"settled_date\":\"2026"
        "-01-07\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":2,\"account_id\""
        ":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"adjustmen"
        "t\",\"amount\":\"20.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null,\"li"
        "nked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-06\",\"settled_date\":\"2026-01-"
        "06\",\"is_imported\":false,\"is_manual_adjustment\":true,\"tags\":[]},{\"id\":1,\"account_id\":1,\"a"
        "ccount_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"ordinary\",\"amo"
        "unt\":\"10.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null,\"linked_ac"
        "count_name\":null,\"recurring_id\":null,\"date\":\"2026-01-05\",\"settled_date\":\"2026-01-05\",\"is"
        "_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]}],\"total\":6,\"limit\":50,\"offset\":"
        "0}"
    ),
    "limit=50&offset=0": (
        "{\"items\":[{\"id\":6,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\""
        "Home\",\"description\":\"leg in\",\"amount\":\"50.00\",\"type\":\"income\",\"status\":\"settled\",\"linked"
        "_transaction_id\":5,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"s"
        "ettled_date\":\"2026-01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\""
        "id\":5,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"descr"
        "iption\":\"leg out\",\"amount\":\"50.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transacti"
        "on_id\":6,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"settled_dat"
        "e\":\"2026-01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":4,\"acc"
        "ount_id\":1,\"account_name\":\"Acct\",\"category_id\":2,\"category_name\":\"Utilities\",\"descriptio"
        "n\":\"sub row\",\"amount\":\"40.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id"
        "\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-08\",\"settled_date\""
        ":\"2026-01-08\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":3,\"accou"
        "nt_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"rej"
        "ected\",\"amount\":\"30.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null"
        ",\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-07\",\"settled_date\":\"2026"
        "-01-07\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":2,\"account_id\""
        ":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"adjustmen"
        "t\",\"amount\":\"20.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null,\"li"
        "nked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-06\",\"settled_date\":\"2026-01-"
        "06\",\"is_imported\":false,\"is_manual_adjustment\":true,\"tags\":[]},{\"id\":1,\"account_id\":1,\"a"
        "ccount_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"ordinary\",\"amo"
        "unt\":\"10.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":null,\"linked_ac"
        "count_name\":null,\"recurring_id\":null,\"date\":\"2026-01-05\",\"settled_date\":\"2026-01-05\",\"is"
        "_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]}],\"total\":6,\"limit\":50,\"offset\":"
        "0}"
    ),
    "sort_by=amount&sort_dir=asc": (
        "{\"items\":[{\"id\":1,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\""
        "Home\",\"description\":\"ordinary\",\"amount\":\"10.00\",\"type\":\"expense\",\"status\":\"settled\",\"lin"
        "ked_transaction_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-"
        "05\",\"settled_date\":\"2026-01-05\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":"
        "[]},{\"id\":2,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\","
        "\"description\":\"adjustment\",\"amount\":\"20.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_"
        "transaction_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-06\","
        "\"settled_date\":\"2026-01-06\",\"is_imported\":false,\"is_manual_adjustment\":true,\"tags\":[]},{"
        "\"id\":3,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"desc"
        "ription\":\"rejected\",\"amount\":\"30.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transac"
        "tion_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-07\",\"settle"
        "d_date\":\"2026-01-07\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":4"
        ",\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":2,\"category_name\":\"Utilities\",\"descr"
        "iption\":\"sub row\",\"amount\":\"40.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transacti"
        "on_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-08\",\"settled_"
        "date\":\"2026-01-08\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":6,\""
        "account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\""
        ":\"leg in\",\"amount\":\"50.00\",\"type\":\"income\",\"status\":\"settled\",\"linked_transaction_id\":5,"
        "\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"settled_date\":\"2026-"
        "01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":5,\"account_id\":"
        "1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"leg out\",\""
        "amount\":\"50.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":6,\"linked_ac"
        "count_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\",\"settled_date\":\"2026-01-09\",\"is"
        "_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]}],\"total\":6,\"limit\":50,\"offset\":"
        "0}"
    ),
    "type=expense&status=settled": (
        "{\"items\":[{\"id\":5,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\""
        "Home\",\"description\":\"leg out\",\"amount\":\"50.00\",\"type\":\"expense\",\"status\":\"settled\",\"link"
        "ed_transaction_id\":6,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-09\","
        "\"settled_date\":\"2026-01-09\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},"
        "{\"id\":4,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":2,\"category_name\":\"Utilities\""
        ",\"description\":\"sub row\",\"amount\":\"40.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_tr"
        "ansaction_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-08\",\"s"
        "ettled_date\":\"2026-01-08\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\""
        "id\":3,\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"descr"
        "iption\":\"rejected\",\"amount\":\"30.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transact"
        "ion_id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-07\",\"settled"
        "_date\":\"2026-01-07\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]},{\"id\":2,"
        "\"account_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description"
        "\":\"adjustment\",\"amount\":\"20.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_"
        "id\":null,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-06\",\"settled_dat"
        "e\":\"2026-01-06\",\"is_imported\":false,\"is_manual_adjustment\":true,\"tags\":[]},{\"id\":1,\"acco"
        "unt_id\":1,\"account_name\":\"Acct\",\"category_id\":1,\"category_name\":\"Home\",\"description\":\"or"
        "dinary\",\"amount\":\"10.00\",\"type\":\"expense\",\"status\":\"settled\",\"linked_transaction_id\":nul"
        "l,\"linked_account_name\":null,\"recurring_id\":null,\"date\":\"2026-01-05\",\"settled_date\":\"202"
        "6-01-05\",\"is_imported\":false,\"is_manual_adjustment\":false,\"tags\":[]}],\"total\":5,\"limit\":"
        "50,\"offset\":0}"
    ),
}

async def _seed_control_org(factory) -> dict:
    """Small, fully deterministic org: fixed dates, first-inserted ids."""
    async with factory() as db:
        org, user, acct = await _base_org(db, name="Control Org")
        home = Category(
            org_id=org.id, name="Home", slug="home", type=CategoryType.EXPENSE
        )
        db.add(home)
        await db.flush()
        utilities = Category(
            org_id=org.id, name="Utilities", slug="utilities",
            type=CategoryType.EXPENSE, parent_id=home.id,
        )
        db.add(utilities)
        await db.flush()

        def _row(cat_id, desc, amount, day, **kw):
            d = date(2026, 1, day)
            return Transaction(
                org_id=org.id, account_id=acct.id, category_id=cat_id,
                description=desc, amount=Decimal(amount),
                type=kw.pop("tx_type", TransactionType.EXPENSE),
                status=TransactionStatus.SETTLED, date=d, settled_date=d, **kw,
            )

        rows = [
            _row(home.id, "ordinary", "10.00", 5),
            _row(home.id, "adjustment", "20.00", 6, is_manual_adjustment=True),
            _row(home.id, "rejected", "30.00", 7, reconciliation_state="rejected"),
            _row(utilities.id, "sub row", "40.00", 8),
        ]
        db.add_all(rows)
        await db.flush()
        leg_a = _row(home.id, "leg out", "50.00", 9)
        leg_b = _row(home.id, "leg in", "50.00", 9, tx_type=TransactionType.INCOME)
        db.add_all([leg_a, leg_b])
        await db.flush()
        leg_a.linked_transaction_id = leg_b.id
        leg_b.linked_transaction_id = leg_a.id
        await db.commit()
        return {"user_id": user.id}


@pytest_asyncio.fixture
async def control(session_factory):
    seed = await _seed_control_org(session_factory)
    app = _make_app(session_factory, seed["user_id"])
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("query", sorted(CONTROL_SNAPSHOTS))
def test_b4_default_response_is_byte_identical_to_main(control, query):
    """CONTROL. Green before AND after PR A by construction — it is red only
    for a change that alters default behaviour on this PAT-reachable endpoint
    (TBD-268 §5), which is the one thing an additive PR must not do."""
    res = control.get(f"/api/v1/transactions?{query}")
    assert res.status_code == 200
    assert res.text == CONTROL_SNAPSHOTS[query]
