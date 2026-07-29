"""Router tests for TBD-234b — `GET /settings/billing-periods/roster`.

Spec: ``specs/2026-07-29-billing-period-roster-design.md`` (revision 7),
§4b tests 15-26 and 32-34. Tests 27-31 are frontend and live elsewhere.

⚠ **There is no `backend/tests/routers/conftest.py`** (spec §4b): every
router test file builds its own FastAPI app, its own StaticPool engine and
its own dependency overrides. The block below is copied from
``tests/routers/test_admin_org_members.py``, which already registers the
``PRAGMA foreign_keys=ON`` connect listener.

House rule for 234b (spec §4): dates are anchored **relative** to
``date.today()``, because this half resolves a real wall clock in the route
(``reference_wall_clock_date_bomb_tests``). The 234a half uses fixed
literals for the opposite reason — it has no clock at all.
"""
from __future__ import annotations

import datetime
import types
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import get_args

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.account import Account, AccountType
from app.models.billing import BillingPeriod
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Organization, Role, User
from app.routers import settings as settings_router_module
from app.routers.settings import router as settings_router
from app.schemas.billing_roster import ANOMALY_MODELS
from app.security import hash_password
from app.services import billing_service

TODAY = datetime.date.today()
DAY = datetime.timedelta(days=1)


def _d(offset: int) -> datetime.date:
    """``TODAY`` shifted by ``offset`` days. Every fixture date goes through
    here so nothing in this file is a literal near the wall clock."""
    return TODAY + datetime.timedelta(days=offset)


# ─── fixture plumbing ────────────────────────────────────────────────────


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


def _make_app(session_factory, user_id_holder: dict) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user(request: Request) -> User:
        request.state.auth_method = "jwt"
        async with session_factory() as db:
            return (
                await db.execute(
                    select(User).where(User.id == user_id_holder["user_id"])
                )
            ).scalar_one()

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(settings_router)
    return app


async def _seed_org(
    factory,
    *,
    periods: list[tuple[datetime.date, datetime.date | None]],
    role: Role = Role.ADMIN,
    is_superadmin: bool = False,
) -> dict:
    """One org, one user, one account, two categories, and `periods`.

    Returns ``{"org_id", "user_id", "account_id", "expense_category_id",
    "income_category_id", "period_ids"}`` where ``period_ids`` is index-aligned
    with ``periods``.
    """
    async with factory() as db:
        org = Organization(name="Roster Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        user = User(
            org_id=org.id,
            username="roster_user",
            email="roster@example.com",
            password_hash=hash_password("pw-1234567"),
            role=role,
            is_superadmin=is_superadmin,
            is_active=True,
            email_verified=True,
        )
        acct_type = AccountType(org_id=org.id, name="Checking", is_system=False)
        db.add_all([user, acct_type])
        await db.flush()

        expense_cat = Category(
            org_id=org.id,
            name="General",
            slug="general",
            type=CategoryType.EXPENSE,
            is_system=False,
        )
        income_cat = Category(
            org_id=org.id,
            name="Salary",
            slug="salary",
            type=CategoryType.INCOME,
            is_system=False,
        )
        db.add_all([expense_cat, income_cat])
        await db.flush()

        account = Account(
            org_id=org.id,
            account_type_id=acct_type.id,
            name="Main",
            balance=Decimal("0.00"),
            currency="EUR",
            is_active=True,
            opening_balance=Decimal("0.00"),
            opening_balance_date=_d(-3650),
        )
        db.add(account)
        await db.flush()

        rows = [
            BillingPeriod(org_id=org.id, start_date=start, end_date=end)
            for start, end in periods
        ]
        db.add_all(rows)
        await db.commit()

        return {
            "org_id": org.id,
            "user_id": user.id,
            "account_id": account.id,
            "expense_category_id": expense_cat.id,
            "income_category_id": income_cat.id,
            "period_ids": [r.id for r in rows],
        }


async def _add_transaction(
    factory,
    seeded: dict,
    *,
    amount: str,
    tx_type: TransactionType = TransactionType.EXPENSE,
    date_: datetime.date,
    settled_date: datetime.date | None,
    status: TransactionStatus = TransactionStatus.SETTLED,
    linked_transaction_id: int | None = None,
    is_manual_adjustment: bool = False,
) -> int:
    async with factory() as db:
        tx = Transaction(
            org_id=seeded["org_id"],
            account_id=seeded["account_id"],
            category_id=(
                seeded["income_category_id"]
                if tx_type is TransactionType.INCOME
                else seeded["expense_category_id"]
            ),
            description="seed",
            amount=Decimal(amount),
            type=tx_type,
            status=status,
            date=date_,
            settled_date=settled_date,
            linked_transaction_id=linked_transaction_id,
            is_manual_adjustment=is_manual_adjustment,
        )
        db.add(tx)
        await db.commit()
        return tx.id


def _client(session_factory, seeded: dict) -> TestClient:
    return TestClient(_make_app(session_factory, {"user_id": seeded["user_id"]}))


def _get(session_factory, seeded: dict, **params) -> tuple[int, dict]:
    with _client(session_factory, seeded) as client:
        resp = client.get("/api/v1/settings/billing-periods/roster", params=params)
        return resp.status_code, (resp.json() if resp.content else {})


def _by_id(payload: dict) -> dict[int, dict]:
    return {p["id"]: p for p in payload["periods"]}


# ─── test 0 (supplementary) — the union is not a third copy of the kinds ──


def test_response_union_is_derived_from_anomaly_kind():
    """Spec §2.5: 234b's Pydantic union is DERIVED from ``AnomalyKind``, or
    ASSERTED against it by a test that fails when the two sets differ.

    ``AnomalyKind`` and ``_KIND_ORDER`` already write the nine kinds down
    twice; a hand-typed third list drifts the moment a tenth kind lands, and
    it drifts silently.
    """
    assert set(ANOMALY_MODELS) == set(get_args(billing_service.AnomalyKind))
    for kind, model in ANOMALY_MODELS.items():
        assert get_args(model.model_fields["kind"].annotation) == (kind,)


# ─── test 15 — the route emits `status` ──────────────────────────────────


@pytest.mark.asyncio
async def test_15_route_emits_status_matching_period_status(session_factory):
    """Test 15 (guard). Thin: the partition itself is fenced by test 12."""
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-90), _d(-61)),   # past
            (_d(-60), _d(-31)),   # past
            (_d(-30), _d(30)),    # current_by_calendar
            (_d(31), None),       # open
            (_d(60), _d(89)),     # upcoming
        ],
    )
    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200

    expected = {}
    for (start, end), pid in zip(
        [
            (_d(-90), _d(-61)),
            (_d(-60), _d(-31)),
            (_d(-30), _d(30)),
            (_d(31), None),
            (_d(60), _d(89)),
        ],
        seeded["period_ids"],
    ):
        expected[pid] = billing_service.period_status(
            billing_service.RosterRow(id=pid, start_date=start, end_date=end),
            today=TODAY,
        )

    rows = _by_id(payload)
    assert set(rows) == set(expected)
    assert {pid: rows[pid]["status"] for pid in rows} == expected
    assert expected[seeded["period_ids"][2]] == "current_by_calendar"
    assert expected[seeded["period_ids"][3]] == "open"
    assert expected[seeded["period_ids"][4]] == "upcoming"


# ─── test 16 — ⭐ the D4 fence ────────────────────────────────────────────


def _freeze_route_clock(monkeypatch, frozen: datetime.date) -> None:
    """Freeze the clock the ROUTE resolves (D8a), not merely pass a kwarg.

    ``period_spend_window_end`` defaults ``today`` to ``date.today()``
    internally, so a route that wires in the floored helper without
    forwarding its own resolved clock is unaffected by a kwarg alone. Swap
    only the NAME ``datetime`` inside the router module — replacing
    ``datetime.date`` process-wide breaks SQLAlchemy's isinstance-based Date
    coercion. ``_FrozenDate`` subclasses ``date``, so every construction and
    comparison the module performs still works.
    """

    class _FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return frozen

    monkeypatch.setattr(
        settings_router_module,
        "datetime",
        types.SimpleNamespace(date=_FrozenDate, timedelta=datetime.timedelta),
    )


@pytest.mark.asyncio
async def test_16_two_ends_and_the_forwarded_clock(session_factory, monkeypatch):
    """Test 16 (⭐ fence). Spec §4b's four normative mechanisms.

    Fixture (all relative to today ``R``), chosen so
    ``T1 <= period_effective_end(open) < T2``::

        P0 [R-200, R-171]  closed
        P1 [R-120, R-91]   closed   → gap P0→P1, R-170..R-121
        P2 [R-90,  NULL ]  OPEN     → derived end = P3.start - 1 = R-61
        P3 [R-60,  R-31 ]  closed

    1. The STRUCTURAL set is equal across the two clocks, marker set named
       explicitly, ``lapsed_open`` EXCLUDED, full payloads including dates.
    2. ``lapsed_open`` LEGITIMATELY DIFFERS: absent at T1, present at T2.
       That is the fence for the route forwarding its resolved clock.
    3. ``effective_end`` comes from ``period_effective_end`` semantics, never
       from ``period_spend_window_end``: at T2 the open row's two ends
       DIVERGE (R-61 vs R), at T1 they AGREE (R-61 vs R-61).
    4. Concrete expected values throughout, never "the two responses match".
    """
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-200), _d(-171)),
            (_d(-120), _d(-91)),
            (_d(-90), None),
            (_d(-60), _d(-31)),
        ],
    )
    p0, p1, p2, _p3 = seeded["period_ids"]
    t1, t2 = _d(-70), _d(0)

    _freeze_route_clock(monkeypatch, t1)
    status_code, at_t1 = _get(session_factory, seeded)
    assert status_code == 200

    _freeze_route_clock(monkeypatch, t2)
    status_code, at_t2 = _get(session_factory, seeded)
    assert status_code == 200

    # ── mechanism 1: the structural set, named explicitly ────────────────
    structural = {
        "gap",
        "overlap",
        "duplicate_open",
        "no_open",
        "inverted",
        "straddling",
    }

    def _structural(payload: dict) -> list[dict]:
        return [a for a in payload["anomalies"] if a["kind"] in structural]

    expected_structural = [
        {
            "kind": "gap",
            "from_period_id": p0,
            "to_period_id": p1,
            "from_date": _d(-170).isoformat(),
            "to_date": _d(-121).isoformat(),
            "off_window": False,
        }
    ]
    assert _structural(at_t1) == expected_structural
    assert _structural(at_t2) == expected_structural

    # ── mechanism 2: `lapsed_open` differs, and that is correct ──────────
    assert [a for a in at_t1["anomalies"] if a["kind"] == "lapsed_open"] == []
    assert [a for a in at_t2["anomalies"] if a["kind"] == "lapsed_open"] == [
        {
            "kind": "lapsed_open",
            "period_id": p2,
            "effective_end": _d(-61).isoformat(),
            "off_window": False,
        }
    ]

    # ── mechanism 3 + 4: the two ends, concrete values ───────────────────
    open_at_t1 = _by_id(at_t1)[p2]
    open_at_t2 = _by_id(at_t2)[p2]

    # Converged clock: `max(R-61, R-70)` is a no-op, the two ends AGREE.
    assert open_at_t1["effective_end"] == _d(-61).isoformat()
    assert open_at_t1["counting_through"] == _d(-61).isoformat()

    # Lapsed clock: the spend window floors at today, the derived end does
    # NOT. Collapsing the two columns is red exactly here.
    assert open_at_t2["effective_end"] == _d(-61).isoformat()
    assert open_at_t2["counting_through"] == _d(0).isoformat()

    # A CLOSED row can never diverge (§2.1): its end is returned verbatim
    # before any floor, at either clock.
    for payload, pid in ((at_t1, p0), (at_t2, p0)):
        row = _by_id(payload)[pid]
        assert row["effective_end"] == _d(-171).isoformat()
        assert row["counting_through"] == _d(-171).isoformat()


# ─── test 17 — overlaps: a transaction counts in EVERY containing row ────


@pytest.mark.asyncio
async def test_17_transaction_counts_in_every_containing_period(session_factory):
    """Test 17 (fence). Kills the single grouped ``CASE`` shape, which
    attributes each row to the FIRST matching period only."""
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-60), _d(-1)),   # A, contains B
            (_d(-40), _d(-20)),  # B
            (_d(1), None),       # open tail, contains nothing seeded
        ],
    )
    a, b, c = seeded["period_ids"]
    await _add_transaction(
        session_factory, seeded, amount="10.00", date_=_d(-30), settled_date=_d(-30)
    )

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    rows = _by_id(payload)
    assert rows[a]["transaction_count"] == 1
    assert rows[b]["transaction_count"] == 1
    assert rows[c]["transaction_count"] == 0
    assert rows[a]["settled_net"] == "-10.00"
    assert rows[b]["settled_net"] == "-10.00"


# ─── test 18 — count unfiltered, settled net filtered ────────────────────


@pytest.mark.asyncio
async def test_18_count_is_unfiltered_and_settled_net_is_filtered(session_factory):
    """Test 18 (fence). D7's two DIFFERENT predicate shapes.

    Excluded rows count toward ``transaction_count`` (which must match the
    click-through set, and ``list_transactions`` applies no
    ``reportable_transaction_filter``) and do NOT count toward ``settled_net``.

    ⚠ **The fixture is asymmetric ON PURPOSE, and a symmetric one is
    vacuous.** A bidirectional transfer pair is equal-and-opposite by
    invariant, so it nets to **zero** — dropping
    ``reportable_transaction_filter`` from ``settled_net`` entirely leaves a
    pair-only fixture GREEN. Verified by injection during the build. The two
    rows below that do not cancel are what make this a fence: a
    reconciliation MATCH (``_apply_match`` writes ``linked_transaction_id``
    **one-way** on the inbox row) and a manual balance adjustment.
    """
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-30), _d(-1)), (_d(0), None)],
    )
    closed, _open = seeded["period_ids"]

    plain = await _add_transaction(
        session_factory, seeded, amount="100.00", date_=_d(-10), settled_date=_d(-10)
    )
    # A bidirectional transfer pair: both legs counted, neither netted.
    leg_out = await _add_transaction(
        session_factory, seeded, amount="40.00", date_=_d(-9), settled_date=_d(-9)
    )
    leg_in = await _add_transaction(
        session_factory,
        seeded,
        amount="40.00",
        tx_type=TransactionType.INCOME,
        date_=_d(-9),
        settled_date=_d(-9),
        linked_transaction_id=leg_out,
    )
    async with session_factory() as db:
        tx = (
            await db.execute(select(Transaction).where(Transaction.id == leg_out))
        ).scalar_one()
        tx.linked_transaction_id = leg_in
        await db.commit()
    # A reconciliation match — one-way link, and it does NOT cancel.
    await _add_transaction(
        session_factory,
        seeded,
        amount="30.00",
        date_=_d(-8),
        settled_date=_d(-8),
        linked_transaction_id=plain,
    )
    # A manual balance adjustment — the filter's other clause.
    await _add_transaction(
        session_factory,
        seeded,
        amount="25.00",
        date_=_d(-7),
        settled_date=_d(-7),
        is_manual_adjustment=True,
    )

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    row = _by_id(payload)[closed]
    # Five rows in the window; every one of them is counted.
    assert row["transaction_count"] == 5
    # Only the plain expense reaches the reportable net. Unfiltered this
    # would be -100 - 30 - 25 = -155.
    assert row["settled_net"] == "-100.00"


# ─── test 19 — future stubs are `upcoming` (no upper bound) ──────────────


@pytest.mark.asyncio
async def test_19_future_stubs_render_as_upcoming(session_factory):
    """Test 19 (fence). D8's window has no upper bound."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-30), None), (_d(30), _d(59)), (_d(60), _d(89))],
    )
    _open, stub1, stub2 = seeded["period_ids"]
    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    rows = _by_id(payload)
    assert rows[stub1]["status"] == "upcoming"
    assert rows[stub2]["status"] == "upcoming"
    assert payload["window"]["to"] is None


# ─── test 20 — off-window markers ────────────────────────────────────────


@pytest.mark.asyncio
async def test_20_off_window_markers_and_referenced_periods(session_factory):
    """Test 20 (fence). Corruption entirely outside the display window is
    still reported: ``off_window`` is true and ``referenced_periods`` carries
    every named id INCLUDING its ``effective_end``.

    ⚠ **The p2→p3 clause below is the fold's C1 fix, and without it the
    ``any()`` in the route is unfenced.** Every off-window marker this file
    used to assert on had **both** ids outside the window, so
    ``any(pid not in displayed_ids ...)`` rewritten as ``all(...)`` stayed
    green. The consequence of the wrong answer is not a mislabelled marker,
    it is an INVISIBLE one: with ``off_window: False`` a straddling gap is
    dropped by ``bandAnomalies`` *and* can never match the rail's
    adjacent-displayed-pair lookup, so it renders nowhere at all — while the
    verdict still counts it. That defeats the page's whole claim that an
    absence of markers means a healthy roster.
    """
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-400), _d(-350)),
            (_d(-300), _d(-250)),
            (_d(-10), None),
        ],
    )
    p1, p2, p3 = seeded["period_ids"]

    status_code, payload = _get(session_factory, seeded, months=1)
    assert status_code == 200

    # Only the open row is displayed.
    assert [p["id"] for p in payload["periods"]] == [p3]

    gap_off = [
        a
        for a in payload["anomalies"]
        if a["kind"] == "gap" and a["from_period_id"] == p1
    ]
    # Both ids off-window — true under `any` AND under `all`.
    assert gap_off == [
        {
            "kind": "gap",
            "from_period_id": p1,
            "to_period_id": p2,
            "from_date": _d(-349).isoformat(),
            "to_date": _d(-301).isoformat(),
            "off_window": True,
        }
    ]

    # ⚠ The STRADDLING gap: `p2` is off-window, `p3` is displayed. `any` says
    # True (one named row cannot be pointed at, so the band must carry it);
    # `all` says False and the marker disappears from the page entirely.
    gap_straddling = [
        a
        for a in payload["anomalies"]
        if a["kind"] == "gap" and a["from_period_id"] == p2
    ]
    assert gap_straddling == [
        {
            "kind": "gap",
            "from_period_id": p2,
            "to_period_id": p3,
            "from_date": _d(-249).isoformat(),
            "to_date": _d(-11).isoformat(),
            "off_window": True,
        }
    ]

    refs = payload["referenced_periods"]
    # In-window ids are INCLUDED, not only the off-window ones.
    assert set(refs) == {str(p1), str(p2), str(p3)}
    assert refs[str(p1)] == {
        "id": p1,
        "start_date": _d(-400).isoformat(),
        "end_date": _d(-350).isoformat(),
        "effective_end": _d(-350).isoformat(),
        "status": "past",
    }
    assert refs[str(p3)]["effective_end"] is None
    assert refs[str(p3)]["status"] == "open"


# ─── test 21 — clamping, truncation direction, analysis untouched ────────


async def _seed_wide_roster(session_factory) -> tuple[dict, list[datetime.date]]:
    """250 rows, 7 days apart, the last one OPEN, with one deliberate gap
    between index 1 and index 2."""
    starts = [_d(-7 * (249 - k)) for k in range(250)]
    periods: list[tuple[datetime.date, datetime.date | None]] = []
    for k, start in enumerate(starts):
        if k == 249:
            periods.append((start, None))
        elif k == 1:
            periods.append((start, start + datetime.timedelta(days=3)))
        else:
            periods.append((start, start + datetime.timedelta(days=6)))
    seeded = await _seed_org(session_factory, periods=periods)
    return seeded, starts


@pytest.mark.asyncio
async def test_21_months_clamped_and_truncation_keeps_the_newest(session_factory):
    """Test 21 (fence). ``months`` clamps to 1..60 rather than 422ing; past
    the 200-row cap ``window.truncated`` is true and the SURVIVORS ARE THE
    NEWEST rows; ``roster.period_count`` still reports the full count and the
    anomaly set is UNCHANGED by truncation.

    ⚠ **The UPPER clamp cannot be fenced on this fixture, and asserting it
    here was vacuous** (fold, C4). 250 rows 7 days apart span ~57 months, so
    every lookback at or above ~48 months admits all 250 rows and the 200-row
    DISPLAY cap binds first — collapsing every such window to the identical
    newest-200 slice. ``over == wide`` therefore could not fail: bracketing
    showed clamps of 12/24/36 caught, 48 and total removal NOT. The upper
    clamp moved to :func:`test_21b_upper_month_clamp`, on a fixture where the
    lookback is the binding constraint. The LOWER clamp stays here and does
    bite: unclamped, ``months=0`` gives ``cutoff = today`` and one row, while
    ``months=1`` gives several.
    """
    seeded, starts = await _seed_wide_roster(session_factory)

    _sc, wide = _get(session_factory, seeded, months=60)
    _sc, narrow = _get(session_factory, seeded, months=1)
    _sc, zero = _get(session_factory, seeded, months=0)

    # Clamped, not rejected, and clamping is what makes this pair equal.
    assert zero == narrow

    # Newest-200, never oldest-200.
    assert wide["window"]["truncated"] is True
    assert wide["window"]["displayed_count"] == 200
    assert len(wide["periods"]) == 200
    assert wide["periods"][0]["start_date"] == starts[50].isoformat()
    assert wide["periods"][-1]["start_date"] == starts[249].isoformat()
    # `window.from` is the TRUNCATED lower bound, not the lookback bound.
    assert wide["window"]["from"] == starts[50].isoformat()

    assert narrow["window"]["truncated"] is False
    assert [p["start_date"] for p in narrow["periods"]] == [
        s.isoformat() for s in starts if s >= TODAY - relativedelta(months=1)
    ]

    # The full count survives both windows.
    assert wide["roster"]["period_count"] == 250
    assert narrow["roster"]["period_count"] == 250
    assert wide["roster"]["first_start"] == starts[0].isoformat()
    assert wide["roster"]["last_start"] == starts[249].isoformat()

    # Display truncation does not touch analysis: the gap at index 1→2 is
    # off-window under BOTH windows, so the payloads are identical.
    assert wide["anomalies"] == narrow["anomalies"]
    assert wide["anomalies"] == [
        {
            "kind": "gap",
            "from_period_id": seeded["period_ids"][1],
            "to_period_id": seeded["period_ids"][2],
            "from_date": (starts[1] + datetime.timedelta(days=4)).isoformat(),
            "to_date": (starts[2] - DAY).isoformat(),
            "off_window": True,
        }
    ]


# ─── test 21b — the UPPER month clamp, on a fixture where it binds ───────


async def _seed_deep_roster(session_factory) -> tuple[dict, list[datetime.date]]:
    """120 contiguous rows, 30 days apart, the last one OPEN.

    Deliberately two things at once, and both are load-bearing for
    :func:`test_21b_upper_month_clamp`:

    * **Span ~117 months**, comfortably past ``ROSTER_MAX_MONTHS``, so the
      60-month lookback genuinely excludes rows.
    * **120 rows, well UNDER ``ROSTER_DISPLAY_CAP``**, so the display slice
      never binds. That is the property test 21's 250-row fixture lacks, and
      it is why the clamp assertion there could not fail.
    """
    starts = [_d(-30 * (119 - k)) for k in range(120)]
    periods: list[tuple[datetime.date, datetime.date | None]] = [
        (start, None if k == 119 else start + datetime.timedelta(days=29))
        for k, start in enumerate(starts)
    ]
    seeded = await _seed_org(session_factory, periods=periods)
    return seeded, starts


@pytest.mark.asyncio
async def test_21b_upper_month_clamp(session_factory):
    """Fold (coverage gap C4). ``months`` clamps DOWN to
    ``ROSTER_MAX_MONTHS``, and the clamped lookback is the thing that decides
    the window.

    Two independent assertions, because one alone is not enough:

    * ``over == wide`` catches REMOVING the upper clamp (999 months admits
      all 120 rows, 60 months admits ~61).
    * The absolute expected slice catches LOWERING the clamp, e.g. to 48 —
      which ``over == wide`` cannot see, since both sides clamp identically.
    """
    seeded, starts = await _seed_deep_roster(session_factory)

    _sc, wide = _get(session_factory, seeded, months=60)
    _sc, over = _get(session_factory, seeded, months=999)

    # The premise: the DISPLAY cap is not what is doing the work here.
    assert wide["window"]["truncated"] is False
    assert 0 < len(wide["periods"]) < settings_router_module.ROSTER_DISPLAY_CAP
    assert len(wide["periods"]) < 120, "the 60-month lookback must exclude rows"

    # Removing the upper clamp lets `months=999` reach further back.
    assert over == wide

    # Lowering the clamp (e.g. to 48) changes the slice, and only an absolute
    # expectation sees that.
    expected = [
        s.isoformat() for s in starts if s >= TODAY - relativedelta(months=60)
    ]
    assert [p["start_date"] for p in wide["periods"]] == expected
    assert wide["window"]["from"] == expected[0]
    assert wide["roster"]["period_count"] == 120


# ─── test 22 — scope separation belt ─────────────────────────────────────


@pytest.mark.asyncio
async def test_22_scope_separation_belt(session_factory):
    """Test 22 (fence, with the clause that makes it bite).

    ``roster.period_count`` equals ``SELECT COUNT(*)``, ``window.displayed_count``
    equals ``len(periods)``, on an org where the two DIFFER — plus an anomaly
    whose subject lies entirely outside the display window, so the test cannot
    pass on a route that counts correctly from one query and analyses a
    windowed list from another.
    """
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-400), _d(-350)),
            (_d(-300), _d(-250)),
            (_d(-10), None),
        ],
    )
    p1, p2, _p3 = seeded["period_ids"]

    status_code, payload = _get(session_factory, seeded, months=1)
    assert status_code == 200

    async with session_factory() as db:
        true_count = (
            await db.execute(
                select(func.count())
                .select_from(BillingPeriod)
                .where(BillingPeriod.org_id == seeded["org_id"])
            )
        ).scalar_one()

    assert payload["roster"]["period_count"] == true_count == 3
    assert payload["window"]["displayed_count"] == len(payload["periods"]) == 1
    assert payload["roster"]["period_count"] != payload["window"]["displayed_count"]

    # The clause that makes it bite.
    assert any(
        a["kind"] == "gap" and a["from_period_id"] == p1 and a["to_period_id"] == p2
        for a in payload["anomalies"]
    )


# ─── test 23 — admin gate ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_23_plain_member_is_forbidden(session_factory):
    """Test 23 (fence). ⚠ The fixture user is a PLAIN MEMBER, not merely a
    non-superadmin: ``_require_admin`` passes OWNER, ADMIN **or**
    ``is_superadmin``, so a non-superadmin ADMIN gets 200 and would prove
    nothing."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-30), None)],
        role=Role.MEMBER,
        is_superadmin=False,
    )
    status_code, _payload = _get(session_factory, seeded)
    assert status_code == 403


@pytest.mark.asyncio
async def test_23b_non_superadmin_admin_is_allowed(session_factory):
    """The other side of the gate, so test 23 cannot pass by refusing
    everyone."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-30), None)],
        role=Role.ADMIN,
        is_superadmin=False,
    )
    status_code, _payload = _get(session_factory, seeded)
    assert status_code == 200


# ─── test 24 — no period is ever manufactured ────────────────────────────


@pytest.mark.asyncio
async def test_24_route_creates_no_period_when_none_is_open(session_factory):
    """Test 24 (fence). Fails the instant anyone reaches for
    ``get_current_period``, which auto-creates AND COMMITS a row."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-60), _d(-31)), (_d(-30), _d(-1))],
    )

    async def _count() -> int:
        async with session_factory() as db:
            return (
                await db.execute(
                    select(func.count())
                    .select_from(BillingPeriod)
                    .where(BillingPeriod.org_id == seeded["org_id"])
                )
            ).scalar_one()

    before = await _count()
    status_code, payload = _get(session_factory, seeded)
    after = await _count()

    assert status_code == 200
    assert before == after == 2
    assert payload["roster"]["period_count"] == 2
    assert [a for a in payload["anomalies"] if a["kind"] == "no_open"] == [
        {"kind": "no_open", "period_ids": [], "off_window": False}
    ]


# ─── test 25 — the empty roster ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_25_empty_roster(session_factory):
    """Test 25 (fence). D10 forbids manufacturing a row to avoid this."""
    seeded = await _seed_org(session_factory, periods=[])
    status_code, payload = _get(session_factory, seeded)

    assert status_code == 200
    assert payload["periods"] == []
    assert payload["referenced_periods"] == {}
    assert payload["anomalies"] == [
        {"kind": "no_open", "period_ids": [], "off_window": False}
    ]
    assert payload["roster"] == {
        "period_count": 0,
        "first_start": None,
        "last_start": None,
        "analyzed": True,
    }
    assert payload["window"] == {
        "from": None,
        "to": None,
        "displayed_count": 0,
        "truncated": False,
    }


# ─── test 26 — the neighbouring route is untouched ───────────────────────


@pytest.mark.asyncio
async def test_26_list_periods_shape_unchanged(session_factory):
    """Test 26 (guard). Regression net: nothing here touches
    ``GET /billing-periods``, which is on the cold-mount critical path."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-60), _d(-31)), (_d(-30), None)],
    )
    with _client(session_factory, seeded) as client:
        resp = client.get("/api/v1/settings/billing-periods")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert all(set(item) == {"id", "start_date", "end_date"} for item in body)


# ─── test 32 — the single-fetch fence ────────────────────────────────────


@pytest.mark.asyncio
async def test_32_one_roster_fetch_only(session_factory):
    """Test 32 (fence, D6/B2/B4). One request issues exactly ONE roster
    ``SELECT`` against ``billing_periods``.

    ⚠ **Spec defect, recorded rather than papered over.** §4b states the
    fence as "exactly one ``SELECT`` against ``billing_periods``", but D6's
    own query budget accepts "~1 per OPEN row for ``counting_through``", and
    that query IS a ``SELECT`` against ``billing_periods``:
    ``period_spend_window_end`` → ``period_effective_end`` →
    ``_next_period_start`` → ``SELECT min(billing_periods.start_date) …
    WHERE start_date > ?``. Taken literally the assertion is RED against a
    correct implementation — B3's defect class. It is therefore built as the
    exact decomposition D6 budgets for: exactly one ROSTER select (no
    aggregate, no LIMIT) plus exactly one ``_next_period_start`` probe per
    displayed OPEN row. Both named red conditions still hold: a second
    windowed ``SELECT`` for the display slice and a re-materialising
    ``select(BillingPeriod)`` for ``counting_through`` each add a
    non-``min()`` statement and trip the first assertion.
    """
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-60), _d(-31)), (_d(-30), _d(-1)), (_d(0), None)],
    )
    await _add_transaction(
        session_factory, seeded, amount="5.00", date_=_d(-40), settled_date=_d(-40)
    )

    statements: list[str] = []

    @event.listens_for(Engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        if "billing_periods" in statement and statement.strip()[:6].upper() in {
            "SELECT",
            "WITH",
        }:
            statements.append(" ".join(statement.split()))

    try:
        status_code, payload = _get(session_factory, seeded)
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    assert status_code == 200
    assert len(payload["periods"]) == 3

    roster_selects = [s for s in statements if "min(billing_periods.start_date)" not in s]
    probe_selects = [s for s in statements if "min(billing_periods.start_date)" in s]

    assert len(roster_selects) == 1, (
        "exactly one roster SELECT against billing_periods is allowed; got "
        f"{len(roster_selects)}: {roster_selects}"
    )
    # `load_complete_roster`'s: three columns, no LIMIT, no date predicate.
    assert "LIMIT" not in roster_selects[0].upper()
    assert "start_date >" not in roster_selects[0]
    # One `_next_period_start` probe per displayed OPEN row, and no more.
    assert len(probe_selects) == 1, probe_selects


# ─── test 33 — the tail row's unbounded aggregate window ─────────────────


@pytest.mark.asyncio
async def test_33_tail_row_aggregates_are_unbounded(session_factory):
    """Test 33 (fence, D7/B1). On a roster whose tail is the open row,
    ``effective_end`` and ``counting_through`` are both null, so BOTH
    aggregate predicates go one-sided: ``>= start_date``, no upper bound."""
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-60), _d(-31)), (_d(-30), None)],
    )
    closed, tail = seeded["period_ids"]

    # Settled, dated in the FUTURE — inside the tail's unbounded window.
    await _add_transaction(
        session_factory, seeded, amount="42.00", date_=_d(45), settled_date=_d(45)
    )
    # Pending with no settled_date — the second UNION ALL branch, also
    # one-sided, counted but not netted.
    await _add_transaction(
        session_factory,
        seeded,
        amount="7.00",
        date_=_d(50),
        settled_date=None,
        status=TransactionStatus.PENDING,
    )
    # A settled row inside the closed period, to prove the bound still binds
    # where there IS one.
    await _add_transaction(
        session_factory, seeded, amount="9.00", date_=_d(-40), settled_date=_d(-40)
    )

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    rows = _by_id(payload)

    assert rows[tail]["effective_end"] is None
    assert rows[tail]["counting_through"] is None
    assert rows[tail]["length_days"] is None
    assert rows[tail]["transaction_count"] == 2
    assert rows[tail]["settled_net"] == "-42.00"

    assert rows[closed]["transaction_count"] == 1
    assert rows[closed]["settled_net"] == "-9.00"


@pytest.mark.asyncio
async def test_33b_aggregates_bound_on_counting_through_not_effective_end(
    session_factory, monkeypatch
):
    """Test 33b (fence, D7/B1) — ⚠ **added during the build, and the spec has
    no test for this.**

    B1's actual ruling is "both aggregate columns bound on
    ``[start_date, counting_through]``", and its stated reason is a LAPSED
    org: bounding on ``effective_end`` instead makes the rendered count
    differ from the linked-to page's count on every lapsed org, destroying
    the "the count and the deep link agree by construction" property. Spec
    test 33 fences only the NULL case (the roster tail, where both bounds are
    ``None`` and the two candidates are indistinguishable), so nothing in
    §4b goes red against the ``effective_end``-bounded variant. This does.

    Fixture: open row ``[R-90, NULL]`` with successor ``[R-60, R-31]``, so
    ``effective_end = R-61`` while ``counting_through = max(R-61, R) = R``.
    Two months of settled rows sit in ``(R-61, R]`` — in the deep link's set,
    and absent from an ``effective_end``-bounded count.
    """
    _freeze_route_clock(monkeypatch, TODAY)
    seeded = await _seed_org(
        session_factory,
        periods=[(_d(-90), None), (_d(-60), _d(-31))],
    )
    lapsed_open, _successor = seeded["period_ids"]

    # Inside [start, effective_end] — counted under either bound.
    await _add_transaction(
        session_factory, seeded, amount="11.00", date_=_d(-80), settled_date=_d(-80)
    )
    # In (effective_end, counting_through] — counted ONLY under the correct
    # bound. This is the row an `effective_end`-bounded aggregate drops.
    await _add_transaction(
        session_factory, seeded, amount="13.00", date_=_d(-20), settled_date=_d(-20)
    )
    # Past `counting_through` — dropped under either bound, so the test
    # cannot pass by simply removing the upper bound altogether.
    await _add_transaction(
        session_factory, seeded, amount="17.00", date_=_d(10), settled_date=_d(10)
    )

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    row = _by_id(payload)[lapsed_open]

    assert row["effective_end"] == _d(-61).isoformat()
    assert row["counting_through"] == _d(0).isoformat()
    assert row["transaction_count"] == 2
    assert row["settled_net"] == "-24.00"


# ─── test 34 — the lookback boundary, both sides ─────────────────────────


@pytest.mark.asyncio
async def test_34_lookback_boundary_both_sides(session_factory):
    """Test 34 (fence, D8/B5). ``cutoff = today - relativedelta(months=months)``
    — not the first of that month — and the predicate is
    ``start_date >= cutoff``, on ``start_date`` alone."""
    cutoff = TODAY - relativedelta(months=12)
    seeded = await _seed_org(
        session_factory,
        periods=[
            (cutoff - DAY, cutoff - DAY),          # one day EARLIER → out
            (cutoff, cutoff + datetime.timedelta(days=29)),  # exactly AT → in
            (cutoff + datetime.timedelta(days=30), None),
        ],
    )
    before, at, tail = seeded["period_ids"]

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200

    shown = [p["id"] for p in payload["periods"]]
    assert at in shown, "a period starting exactly at the cutoff is IN window"
    assert before not in shown, "a period starting one day earlier is OUT"
    assert shown == [at, tail]
    assert payload["window"]["from"] == cutoff.isoformat()
    assert payload["roster"]["period_count"] == 3


# ─── fold: coverage gaps proven by injection during PR review ────────────


@pytest.mark.asyncio
async def test_35_length_days_is_the_inclusive_span(session_factory):
    """Fold (coverage gap C2). ``length_days`` was unfenced end to end.

    Both an off-by-one (dropping ``_roster_length_days``' inclusive ``+ 1``)
    and a hardcoded ``None`` passed the entire backend suite, because the
    only assertion on the field anywhere was ``is None`` on the roster tail —
    which both wrong implementations also satisfy. The frontend renders
    ``${length_days} days`` with no assertion either, so a silently
    off-by-one period length would have shipped visible on every row.
    """
    seeded = await _seed_org(
        session_factory,
        periods=[
            (_d(-90), _d(-61)),   # closed: 30 days inclusive
            (_d(-60), _d(-60)),   # closed, a SINGLE day: 1, never 0
            (_d(-40), _d(-50)),   # end before start -> `invalid`
            (_d(-10), None),      # roster tail: `effective_end` is null
        ],
    )
    thirty, one_day, invalid, tail = seeded["period_ids"]

    status_code, payload = _get(session_factory, seeded)
    assert status_code == 200
    rows = _by_id(payload)

    # Inclusive. Dropping the `+ 1` makes this 29.
    assert rows[thirty]["length_days"] == 30
    # The one-day row is where an off-by-one is unmistakable: 1, never 0.
    assert rows[one_day]["length_days"] == 1
    # `invalid` suppresses it: the span is negative and the status already
    # carries the signal.
    assert rows[invalid]["status"] == "invalid"
    assert rows[invalid]["length_days"] is None
    # Nothing bounds the tail, so there is no span to state.
    assert rows[tail]["effective_end"] is None
    assert rows[tail]["length_days"] is None


@pytest.mark.asyncio
async def test_36_analyzed_is_false_when_the_overlap_check_refuses(session_factory):
    """Fold (coverage gap C3). ``roster.analyzed`` was unfenced.

    Hardcoding it ``True`` passed the whole suite: no route-level test ever
    produced ``overlap_analysis_skipped``. The wrong value is user-visible on
    exactly the large corrupted rosters this page exists for — the page
    renders "Ran"/"Skipped" AND swaps the guarantee sentence, so a hardcoded
    ``True`` makes the page claim "Checks cover your entire roster" on the one
    roster where that is false.

    ``analyzed is True`` is fenced on the other side by tests 20/21/25.

    The rows are placed far in the past so a default 12-month window displays
    NONE of them: the assertion is about analysis, and per-row aggregates on
    200 displayed rows would cost ~400 queries for nothing.
    """
    cap = billing_service.OVERLAP_ANALYSIS_CAP
    base = -(cap + 500)
    seeded = await _seed_org(
        session_factory,
        # Contiguous one-day rows, so nothing here emits a gap or an overlap
        # and the skipped marker is unambiguous.
        periods=[(_d(base + k), _d(base + k)) for k in range(cap + 1)],
    )
    assert len(seeded["period_ids"]) == cap + 1

    status_code, payload = _get(session_factory, seeded, months=1)
    assert status_code == 200

    assert payload["roster"]["period_count"] == cap + 1
    # ⚠ The fence.
    assert payload["roster"]["analyzed"] is False
    # …and it is a RESTATEMENT of a marker the kernel emits, never a second
    # source of truth, so the marker must be on the wire too.
    assert [
        a for a in payload["anomalies"] if a["kind"] == "overlap_analysis_skipped"
    ] == [
        {
            "kind": "overlap_analysis_skipped",
            "period_count": cap + 1,
            "cap": cap,
            "off_window": False,
        }
    ]
    # Every other rule still ran over the complete roster.
    assert any(a["kind"] == "no_open" for a in payload["anomalies"])
    assert payload["periods"] == []


@pytest.mark.asyncio
async def test_37_nullable_wire_fields_are_required_not_optional(session_factory):
    """Fold (backend review, item 1). Every nullable field on this wire is
    **required-and-nullable**, never optional.

    ``WindowScope.from``/``to`` used to carry Pydantic defaults, which makes
    them OPTIONAL in the generated OpenAPI schema while every sibling nullable
    field is required. A generated TS client then types ``window.from`` as
    ``string | undefined``, and a consumer cannot distinguish §2.5's
    legitimate empty-display-window case from "the field is absent".
    """
    seeded = await _seed_org(session_factory, periods=[(_d(-30), None)])
    with _client(session_factory, seeded) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schemas["WindowScope"]["required"]) == {
        "from",
        "to",
        "displayed_count",
        "truncated",
    }
    # The siblings this was made consistent with.
    assert {"end_date", "effective_end", "counting_through", "length_days"} <= set(
        schemas["RosterPeriod"]["required"]
    )
    assert {"end_date", "effective_end"} <= set(
        schemas["ReferencedPeriod"]["required"]
    )
    # `roster.first_start` / `last_start` were already required-and-nullable.
    assert {"first_start", "last_start"} <= set(schemas["RosterScope"]["required"])


@pytest.mark.asyncio
async def test_38_months_bounds_are_documented_without_being_enforced(session_factory):
    """Fold (backend review, item 2). The clamp is DOCUMENTED in OpenAPI, and
    documenting it must not turn it into a rejection.

    ``ge``/``le`` on the query param would 422 a ``months=600`` that D6/D8 say
    to serve as 60, so the bounds live in the description only. This asserts
    both halves, because adding the constraint would satisfy the first alone.
    """
    seeded = await _seed_org(session_factory, periods=[(_d(-30), None)])
    with _client(session_factory, seeded) as client:
        spec = client.get("/openapi.json").json()
    params = spec["paths"]["/api/v1/settings/billing-periods/roster"]["get"][
        "parameters"
    ]
    months = next(p for p in params if p["name"] == "months")
    assert months["required"] is False
    assert months["schema"]["default"] == 12
    assert "1..60" in months["description"]
    # ⚠ Documented, NOT enforced: no `minimum`/`maximum` may appear, or the
    # clamp-don't-reject ruling has been inverted.
    assert "minimum" not in months["schema"]
    assert "maximum" not in months["schema"]

    # And the behaviour still clamps rather than rejects.
    for value in (-5, 0, 600, 10**9):
        status_code, _payload = _get(session_factory, seeded, months=value)
        assert status_code == 200, value
    with _client(session_factory, seeded) as client:
        assert (
            client.get(
                "/api/v1/settings/billing-periods/roster", params={"months": "abc"}
            ).status_code
            == 422
        )
