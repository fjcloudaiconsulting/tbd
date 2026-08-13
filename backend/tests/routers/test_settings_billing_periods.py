"""Route-level coverage for the billing-period endpoints in
``routers/settings.py`` (TBD-232, slice 1 of the TBD-213 split; extended
by TBD-239).

Before this file the router had **zero** billing coverage: three write
paths (``PUT /billing-cycle``, ``POST /billing-period``,
``POST /billing-period/close``) that mutate period boundaries and budget
anchors were exercised only indirectly through the service layer.

TBD-239 removed the ``PUT /billing-cycle`` re-anchor entirely (a cycle-day
change now applies from the next period) and gave the two remaining
boundary producers an **intersection** guard instead of an exact-start
one. The re-anchor's router-level tests went with it; the direct
``reanchor_period_dependents`` service tests at the bottom of this file
stay, because TBD-235 and TBD-241 are its named future consumers.

Harness note — load-bearing
---------------------------
These tests mount the router on a bare ``FastAPI()`` (following
``test_settings_forecast_granularity.py:54-71``), so ``main.py``'s
app-level exception handlers are **not** registered. Without re-declaring
them here an uncaught ``ConflictError`` re-raises straight through
``TestClient`` instead of becoming a 409, and every status assertion
below would fail for the wrong reason. We follow ``test_tags.py:124-139``
but **include ``code`` in the ``ConflictError`` body**, which that file
omits — the new ``budget_period_conflict`` / ``billing_period_exists``
codes are precisely what the frontend branches on.
"""
from __future__ import annotations

import datetime
import types
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest import mock

import pytest
import pytest_asyncio
import structlog.testing
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import seed
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.forecast_plan import ForecastPlan
from app.models.user import Organization, Role, User
from app.routers import settings as settings_module
from app.routers.settings import router as settings_router
from app.security import hash_password
from app.services import billing_service, budget_service
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


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


def _make_app(session_factory, user_id: int):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one()

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(settings_router)

    # main.py's domain-exception handlers are not present on a bare app.
    # Re-register them (test_tags.py:124-139) so the router's ConflictError
    # surfaces as a 409 — and unlike test_tags.py, carry `code` through,
    # because that is what these assertions are about.
    @app.exception_handler(NotFoundError)
    async def _nf(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(request, exc):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(request, exc):
        content = {"detail": exc.detail}
        if getattr(exc, "code", None):
            content["code"] = exc.code
        return JSONResponse(status_code=409, content=content)

    return app


async def _seed(factory, *, cycle_day: int = 1) -> dict:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=cycle_day)
        db.add(org)
        await db.commit()
        owner = User(
            org_id=org.id, username="owner", email="owner@acme.io",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_active=True, email_verified=True,
        )
        member = User(
            org_id=org.id, username="member", email="member@acme.io",
            password_hash=hash_password("pw-1234567"), role=Role.MEMBER,
            is_active=True, email_verified=True,
        )
        groceries = Category(org_id=org.id, name="Groceries", slug="groceries")
        transport = Category(org_id=org.id, name="Transport", slug="transport")
        db.add_all([owner, member, groceries, transport])
        await db.commit()
        return {
            "org": org.id,
            "owner": owner.id,
            "member": member.id,
            "groceries": groceries.id,
            "transport": transport.id,
        }


async def _add_period(factory, org_id, start, end=None) -> int:
    async with factory() as db:
        period = BillingPeriod(org_id=org_id, start_date=start, end_date=end)
        db.add(period)
        await db.commit()
        return period.id


async def _add_budget(factory, org_id, category_id, start, end=None) -> int:
    async with factory() as db:
        budget = Budget(
            org_id=org_id, category_id=category_id,
            amount=Decimal("100.00"), period_start=start, period_end=end,
        )
        db.add(budget)
        await db.commit()
        return budget.id


async def _budgets(factory, org_id) -> list[Budget]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(Budget)
                    .where(Budget.org_id == org_id)
                    .order_by(Budget.id)
                )
            ).scalars().all()
        )


async def _audit_rows(factory, event_type: str | None = None) -> list[AuditEvent]:
    async with factory() as db:
        stmt = select(AuditEvent).order_by(AuditEvent.id)
        if event_type:
            stmt = stmt.where(AuditEvent.event_type == event_type)
        return list((await db.execute(stmt)).scalars().all())


async def _periods(factory, org_id) -> list[BillingPeriod]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.org_id == org_id)
                    .order_by(BillingPeriod.start_date)
                )
            ).scalars().all()
        )


async def _roster(factory, org_id) -> list[tuple]:
    """(id, start_date, end_date) for every period, ordered by start."""
    return [(p.id, p.start_date, p.end_date) for p in await _periods(factory, org_id)]


def _freeze_today(monkeypatch, frozen: datetime.date) -> None:
    """Pin ``datetime.date.today()`` for the router and the billing service.

    Load-bearing for the seed-shape case: the branch that used to orphan a
    day is ``today.day < 25``, and the real wall clock spends most of the
    month in the other branch, where the old code was a no-op. An unfrozen
    test there proves nothing.

    Patches the module-level ``datetime`` name rather than the stdlib type
    (``datetime.date`` is a C type and rejects ``setattr``), so the freeze
    is scoped to the two modules under test.
    """

    class _Date(datetime.date):
        @classmethod
        def today(cls) -> datetime.date:
            return frozen

    shim = types.SimpleNamespace(
        date=_Date,
        timedelta=datetime.timedelta,
        datetime=datetime.datetime,
    )
    monkeypatch.setattr(settings_module, "datetime", shim)
    monkeypatch.setattr(billing_service, "datetime", shim)


def _intersecting_pairs(periods: list[BillingPeriod]) -> list[tuple]:
    """Every pair of rows whose [start, end] windows overlap.

    Rows with a NULL ``end_date`` are compared on their start date alone —
    an open row's true extent is unknowable, which is exactly why the
    production predicates ignore it.
    """
    bad = []
    for i, a in enumerate(periods):
        for b in periods[i + 1:]:
            a_end = a.end_date or a.start_date
            b_end = b.end_date or b.start_date
            if a.start_date <= b_end and b.start_date <= a_end:
                bad.append((a.start_date, a_end, b.start_date, b_end))
    return bad


# ── POST /billing-period ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_period_with_valid_body_returns_200(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_date"] == "2026-03-01"
    assert body["end_date"] == "2026-03-31"

    async with session_factory() as db:
        row = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == ids["org"])
            )
        ).scalar_one()
    assert row.start_date == datetime.date(2026, 3, 1)


@pytest.mark.asyncio
async def test_create_period_without_start_date_returns_422(session_factory):
    """Regression: `start_date: datetime.date = None` as a query param let
    None through to a NOT NULL column and 500ed at commit."""
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post("/api/v1/settings/billing-period", json={})

    assert resp.status_code == 422, resp.text
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.org_id == ids["org"])
            )
        ).scalars().all()
    assert list(rows) == []


@pytest.mark.asyncio
async def test_create_period_with_end_before_start_returns_422(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-10", "end_date": "2026-03-01"},
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_period_duplicate_start_returns_409(session_factory):
    ids = await _seed(session_factory)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 3, 1))
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-01"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "billing_period_exists"


@pytest.mark.asyncio
async def test_create_period_rejects_non_admin(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["member"]))

    resp = client.post(
        "/api/v1/settings/billing-period", json={"start_date": "2026-03-01"}
    )

    assert resp.status_code == 403


# TBD-239 §3 — containment. Case 7 of the spec's Testing section.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "label"),
    [
        ("2026-03-10", "2026-03-20", "fully contained"),
        ("2026-02-20", "2026-03-10", "straddles the start"),
        ("2026-03-20", "2026-04-10", "straddles the end"),
        ("2026-02-01", "2026-04-30", "swallows it whole"),
        ("2026-03-31", "2026-04-30", "touches the last day"),
    ],
)
async def test_create_period_overlapping_window_returns_409(
    session_factory, start, end, label
):
    ids = await _seed(session_factory)
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": start, "end_date": end},
    )

    assert resp.status_code == 409, f"{label}: {resp.text}"
    body = resp.json()
    assert body["code"] == "billing_period_overlap"
    assert body["detail"] == (
        "A billing period already covers 2026-03-01 to 2026-03-31. "
        "Choose dates outside that range."
    )

    # Nothing was inserted.
    assert len(await _periods(session_factory, ids["org"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-04-01", "2026-04-30"),  # abuts the end, no shared day
        ("2026-01-01", "2026-02-28"),  # abuts the start, no shared day
    ],
)
async def test_create_period_adjacent_window_is_allowed(session_factory, start, end):
    """Boundaries are INCLUSIVE, so touching is not overlapping. A period
    that starts the day after the neighbour ends is the contiguous roster
    the whole ticket is trying to preserve."""
    ids = await _seed(session_factory)
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": start, "end_date": end},
    )

    assert resp.status_code == 200, resp.text
    assert _intersecting_pairs(await _periods(session_factory, ids["org"])) == []


@pytest.mark.asyncio
async def test_create_period_duplicate_start_still_wins_over_overlap(session_factory):
    """The exact-start check keeps its first position.

    A same-day `./pfv seed` re-run posts start dates that already exist and
    branches on `billing_period_exists`; if the containment check ran first
    it would answer `billing_period_overlap` for the same rows and the seed
    helper's older branch would stop absorbing it.
    """
    ids = await _seed(session_factory)
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "billing_period_exists"


@pytest.mark.asyncio
async def test_create_open_period_is_checked_on_its_start_date_alone(session_factory):
    """A candidate with no `end_date` is NOT unbounded.

    `seed.py`'s current-open-period POST posts exactly this shape for the current open period.
    Treating it as extending to infinity would make seeding an open period
    after any closed period conflict every time.
    """
    ids = await _seed(session_factory)
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 3, 1), datetime.date(2026, 3, 31),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    # Start date falls inside the closed window -> conflict.
    inside = client.post(
        "/api/v1/settings/billing-period", json={"start_date": "2026-03-15"}
    )
    assert inside.status_code == 409, inside.text
    assert inside.json()["code"] == "billing_period_overlap"

    # Start date after it -> allowed, even though the candidate has no end.
    after = client.post(
        "/api/v1/settings/billing-period", json={"start_date": "2026-04-01"}
    )
    assert after.status_code == 200, after.text
    assert after.json()["end_date"] is None


@pytest.mark.asyncio
async def test_create_period_ignores_existing_open_rows(session_factory):
    """An existing row with `end_date IS NULL` has an unknowable END, so the
    part of the overlap question that depends on that end is waved through.
    Here the open row starts BEFORE the candidate window, so nothing about
    the collision is provable and the insert is allowed."""
    ids = await _seed(session_factory)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 3, 1))
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-15", "end_date": "2026-04-14"},
    )

    assert resp.status_code == 200, resp.text
    assert len(await _periods(session_factory, ids["org"])) == 2


@pytest.mark.asyncio
async def test_create_period_rejects_swallowing_an_open_rows_start(session_factory):
    """The other half of the open-row rule (TBD-239 review F3).

    An open row's END is unknowable, but its START is not, and the
    candidate's window is fully known. An open row whose start falls inside
    [candidate.start, candidate_end] is therefore a PROVABLE overlap. An
    earlier revision skipped open rows entirely and let this land: repeated
    `./pfv seed` runs produced closed rows that swallowed an open row's
    start.
    """
    ids = await _seed(session_factory)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 3, 20))
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": "2026-03-15", "end_date": "2026-04-14"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "billing_period_overlap"
    # The message names the row it collided with, and does not print `None`
    # where a closed row would have printed an end date.
    assert "2026-03-20" in resp.json()["detail"]
    assert "None" not in resp.json()["detail"]
    # Nothing was inserted.
    assert len(await _periods(session_factory, ids["org"])) == 1


@pytest.mark.asyncio
async def test_create_open_period_rejects_a_later_open_rows_start(session_factory):
    """Same rule with an open CANDIDATE: its window is the single day
    `start_date`, so only an open row starting on that exact day collides,
    and that is already the exact-start check's job. One day later is
    outside the candidate window and must still be allowed."""
    ids = await _seed(session_factory)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 3, 21))
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post(
        "/api/v1/settings/billing-period", json={"start_date": "2026-03-20"}
    )

    assert resp.status_code == 200, resp.text
    assert len(await _periods(session_factory, ids["org"])) == 2


# ── POST /billing-period/close ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_period_writes_audit_row(session_factory):
    ids = await _seed(session_factory)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=10)
    period_id = await _add_period(session_factory, ids["org"], start)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.post("/api/v1/settings/billing-period/close")

    assert resp.status_code == 200, resp.text
    new_start = datetime.date.fromisoformat(resp.json()["start_date"])
    # `close_period` defaults to "close yesterday", so the new period opens
    # today. Asserted against the clock, not against the router's own
    # response, so the audited close date is pinned to something independent.
    assert new_start == today
    yesterday = today - datetime.timedelta(days=1)

    # The closing period really was closed at that date.
    async with session_factory() as db:
        closed = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.id == period_id)
            )
        ).scalar_one()
    assert closed.end_date == yesterday

    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.target_org_id == ids["org"]
    assert row.actor_email == "owner@acme.io"
    assert row.detail["closed_period_id"] == period_id
    assert row.detail["closed_period_start"] == start.isoformat()
    # Resolved from the NEW period's start, never re-derived in the router —
    # and it matches the end_date actually written on the closed row.
    assert row.detail["close_date"] == yesterday.isoformat()
    assert row.detail["new_period_start"] == today.isoformat()


@pytest.mark.asyncio
async def test_close_period_audits_non_validation_failures(session_factory):
    """`close_period` can also raise RuntimeError (its row vanished after an
    IntegrityError retry) or IntegrityError from its second commit. Catching
    only ValidationError left those as unaudited 500s."""
    ids = await _seed(session_factory)
    start = datetime.date.today() - datetime.timedelta(days=5)
    period_id = await _add_period(session_factory, ids["org"], start)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    async def _boom(db, org_id, close_date=None):
        raise RuntimeError("period vanished")

    with mock.patch.object(billing_service, "close_period", _boom):
        with pytest.raises(RuntimeError):
            client.post("/api/v1/settings/billing-period/close")

    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail["reason"] == "error"
    assert rows[0].detail["error_type"] == "RuntimeError"
    assert rows[0].detail["closed_period_id"] == period_id


@pytest.mark.asyncio
async def test_close_period_writes_failure_audit_row_on_rejection(session_factory):
    """close_date before the period start is a domain ValidationError -> 400,
    and must still leave an audit trail."""
    ids = await _seed(session_factory)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=3)
    await _add_period(session_factory, ids["org"], start)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    bad = (start - datetime.timedelta(days=1)).isoformat()
    resp = client.post(f"/api/v1/settings/billing-period/close?close_date={bad}")

    assert resp.status_code == 400, resp.text
    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail["reason"] == "validation"


@pytest.mark.asyncio
async def test_close_period_rejects_non_admin(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["member"]))

    resp = client.post("/api/v1/settings/billing-period/close")

    assert resp.status_code == 403
    assert await _audit_rows(session_factory) == []


# ── TBD-241: chain-close, through the FK-enforcing harness ────────────────
#
# Spec: specs/2026-07-28-close-period-chain-close-design.md, §5 tests 14-17.
#
# House rule: FK-sensitive assertions belong in THIS file, whose fixture sets
# `PRAGMA foreign_keys=ON` (`:75`). `tests/services/test_billing_service.py`
# does not, so an FK violation passes there undetected — which is exactly what
# tests 14 and 15 exist to catch.
#
# Tests 14 and 15 carry a GUARD as their purpose but are also regression
# fences: both FAIL against `main`. Test 14 expects the response to open at
# 2026-05-25 where `main` jumps the whole way and opens 2026-07-25; test 15
# expects the revived row's budget to carry `period_end is None` where `main`
# leaves the stale 2026-06-24 snapshot. *(The spec, and the first draft of this
# comment, called them "guards that pass against main". Checked against `main`
# during the code review — they do not. Corrected, F6.)*
#
# The guard half is what they were written for: the design that was rejected in
# favour of the clamp absorbed intervening periods by DELETEing them, and
# `forecast_plans.billing_period_id` is NOT NULL with no `ondelete`
# (`models/forecast_plan.py:43`), so InnoDB would RESTRICT -> MySQL 1451 -> an
# unhandled 500. `Budget` has no FK at all and `period_start` is its sole join
# key, so a deleted period would strand its budgets: invisible via
# `list_budgets` (which swallows the error and returns `[]`) while still
# occupying `uq_budget_org_cat_period`.
#
# Test 17 is `test_close_period_audits_non_validation_failures` above: its
# `_boom(db, org_id, close_date=None)` monkeypatch still matches because D2
# made `today` KEYWORD-ONLY and the router call site deliberately does not pass
# it.


async def _lapsed_roster(factory) -> dict:
    """Open period two cycles behind plus two intact stubs, cycle day 25.

    The §0 roster: an org whose open period has lapsed, with stubs a Forecasts
    mount already created ahead of it.
    """
    ids = await _seed(factory, cycle_day=25)
    ids["open"] = await _add_period(factory, ids["org"], datetime.date(2026, 4, 25))
    ids["stub_1"] = await _add_period(
        factory, ids["org"], datetime.date(2026, 5, 25), datetime.date(2026, 6, 24)
    )
    ids["stub_2"] = await _add_period(
        factory, ids["org"], datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)
    )
    return ids


@pytest.mark.asyncio
async def test_clamped_close_leaves_a_stubs_forecast_plan_intact(
    session_factory, monkeypatch
):
    """§5 test 14 — no row is deleted, so no FK is ever restricted."""
    _freeze_today(monkeypatch, datetime.date(2026, 7, 28))
    ids = await _lapsed_roster(session_factory)
    async with session_factory() as db:
        plan = ForecastPlan(org_id=ids["org"], billing_period_id=ids["stub_2"])
        db.add(plan)
        await db.commit()
        plan_id = plan.id

    client = TestClient(_make_app(session_factory, ids["owner"]))
    resp = client.post(
        "/api/v1/settings/billing-period/close?close_date=2026-07-24"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["start_date"] == "2026-05-25"

    async with session_factory() as db:
        plan_row = (
            await db.execute(select(ForecastPlan).where(ForecastPlan.id == plan_id))
        ).scalar_one()
        assert plan_row.billing_period_id == ids["stub_2"]
    assert await _roster(session_factory, ids["org"]) == [
        (ids["open"], datetime.date(2026, 4, 25), datetime.date(2026, 5, 24)),
        (ids["stub_1"], datetime.date(2026, 5, 25), None),
        (ids["stub_2"], datetime.date(2026, 6, 25), datetime.date(2026, 7, 24)),
    ]


@pytest.mark.asyncio
async def test_clamped_close_leaves_stub_budgets_reachable(
    session_factory, monkeypatch
):
    """§5 test 15 — every intervening period survives AS the closed period the
    user planned, with its budgets still joinable at its own start."""
    _freeze_today(monkeypatch, datetime.date(2026, 7, 28))
    ids = await _lapsed_roster(session_factory)
    revived_budget = await _add_budget(
        session_factory, ids["org"], ids["groceries"],
        datetime.date(2026, 5, 25), datetime.date(2026, 6, 24),
    )
    await _add_budget(
        session_factory, ids["org"], ids["groceries"],
        datetime.date(2026, 6, 25), datetime.date(2026, 7, 24),
    )

    client = TestClient(_make_app(session_factory, ids["owner"]))
    resp = client.post(
        "/api/v1/settings/billing-period/close?close_date=2026-07-24"
    )
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        untouched = await budget_service.list_budgets(
            db, ids["org"], datetime.date(2026, 6, 25)
        )
    assert [b.category_id for b in untouched] == [ids["groceries"]]
    assert untouched[0].period_end == datetime.date(2026, 7, 24)

    # D5's mirror: the revived row is open again, so its budgets' stored
    # `period_end` snapshot is cleared rather than left describing a window
    # that no longer ends.
    assert (await _budgets(session_factory, ids["org"]))[0].id == revived_budget
    async with session_factory() as db:
        revived = await budget_service.list_budgets(
            db, ids["org"], datetime.date(2026, 5, 25)
        )
    assert revived[0].period_end is None


@pytest.mark.asyncio
async def test_clamped_close_audits_the_clamped_and_the_requested_date(
    session_factory, monkeypatch
):
    """§5 test 16 — D10's audit mechanism.

    The audit key `close_date` is derived from the NEW period's start
    (`settings.py:568`), never echoed from the parameter, so it already reports
    the CLAMPED date with no code change. `requested_close_date` is added as a
    verbatim echo of the raw parameter — null when absent, which under the
    current UI means null on every human close. The clamp signal itself is the
    service's structured event.
    """
    _freeze_today(monkeypatch, datetime.date(2026, 7, 28))
    ids = await _lapsed_roster(session_factory)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    with structlog.testing.capture_logs() as logs:
        resp = client.post(
            "/api/v1/settings/billing-period/close?close_date=2026-07-24"
        )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["close_date"] == "2026-05-24", "the CLAMPED date"
    assert detail["requested_close_date"] == "2026-07-24"
    assert detail["closed_period_id"] == ids["open"]
    assert detail["new_period_start"] == "2026-05-25"

    clamped = [e for e in logs if e.get("event") == "billing.close.clamped"]
    assert len(clamped) == 1
    assert clamped[0]["requested_close_date"] == "2026-07-24"
    assert clamped[0]["clamped_to"] == "2026-05-24"
    assert clamped[0]["absorbed_period_ids"] == [ids["stub_1"], ids["stub_2"]]
    assert clamped[0]["revived_period_id"] == ids["stub_1"]


@pytest.mark.asyncio
async def test_unclamped_close_audits_a_null_requested_date(
    session_factory, monkeypatch
):
    """D10's honest limitation, pinned: the UI sends no `close_date`, so the
    echo is null for every human close and the audit row alone cannot
    distinguish "asked for 07-27" from "asked for nothing"."""
    _freeze_today(monkeypatch, datetime.date(2026, 7, 28))
    ids = await _seed(session_factory, cycle_day=25)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 7, 25))
    client = TestClient(_make_app(session_factory, ids["owner"]))

    assert client.post("/api/v1/settings/billing-period/close").status_code == 200

    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert rows[0].detail["requested_close_date"] is None
    assert rows[0].detail["close_date"] == "2026-07-27"


# ── PUT /billing-cycle ────────────────────────────────────────────────────
#
# TBD-239 §1 deleted the re-anchor. The fixture below is deliberately
# OFF-GRID — an open period starting on the 2nd while the cycle day is the
# 15th — because that is the shape every org that has ever closed manually
# already has (`close_period` closes yesterday), and it is the shape the
# deleted code silently rewrote.

_FROZEN_TODAY = datetime.date(2026, 3, 20)
_OFF_GRID_CLOSED = (datetime.date(2026, 2, 2), datetime.date(2026, 3, 1))
_OFF_GRID_OPEN = datetime.date(2026, 3, 2)


async def _seed_off_grid(session_factory) -> dict:
    """Org on cycle day 15 whose open period starts on the 2nd.

    With ``_FROZEN_TODAY``, the deleted re-anchor would have moved that
    start to 2026-03-05 for cycle day 5 (forward) and to 2026-02-25 for
    cycle day 25 (backward, straight into the middle of the closed
    predecessor).
    """
    ids = await _seed(session_factory, cycle_day=15)
    ids["closed_period"] = await _add_period(
        session_factory, ids["org"], *_OFF_GRID_CLOSED
    )
    ids["open_period"] = await _add_period(
        session_factory, ids["org"], _OFF_GRID_OPEN
    )
    return ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_day", "direction"), [(5, "forward"), (25, "backward")]
)
async def test_update_billing_cycle_mutates_no_period_rows(
    session_factory, monkeypatch, new_day, direction
):
    """Case 1 — a cycle-day change in EITHER direction touches zero rows.

    The old handler re-rooted the open period's ``start_date`` in place and
    dragged its budgets along, which opened a gap (forward) or an overlap
    with the closed predecessor (backward). Now it writes the org column
    and nothing else; the grid change lands at the next close.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed_off_grid(session_factory)
    budget_id = await _add_budget(
        session_factory, ids["org"], ids["groceries"], _OFF_GRID_OPEN
    )
    before = await _roster(session_factory, ids["org"])
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": new_day}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"billing_cycle_day": new_day}

    # Every id, start and end is byte-for-byte what it was, and no row was
    # added or removed. `direction` only names which way the old code moved.
    assert await _roster(session_factory, ids["org"]) == before

    # Budgets stayed anchored to the period they were budgeted for.
    budgets = await _budgets(session_factory, ids["org"])
    assert [(b.id, b.period_start, b.period_end) for b in budgets] == [
        (budget_id, _OFF_GRID_OPEN, None)
    ]

    # The cycle day itself did stick — it is the whole write.
    async with session_factory() as db:
        org = (
            await db.execute(
                select(Organization).where(Organization.id == ids["org"])
            )
        ).scalar_one()
    assert org.billing_cycle_day == new_day


@pytest.mark.asyncio
async def test_update_billing_cycle_writes_audit_row(session_factory, monkeypatch):
    """Case 2 — the audit payload describes deferral, not a move."""
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed_off_grid(session_factory)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": 25}
    )

    assert resp.status_code == 200, resp.text
    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.detail["old_day"] == 15
    assert row.detail["new_day"] == 25
    assert row.detail["period_id"] == ids["open_period"]
    assert row.detail["open_period_start"] == _OFF_GRID_OPEN.isoformat()
    assert row.detail["applies_from"] == "next_period"
    # The three keys that described the deleted move are gone. Left behind
    # they would keep telling /admin/audit a boundary moved.
    assert "budgets_reanchored" not in row.detail
    assert "new_start" not in row.detail
    assert "old_start" not in row.detail


@pytest.mark.asyncio
async def test_update_billing_cycle_leaves_no_gap_on_the_seed_shape(
    session_factory, monkeypatch
):
    """Case 3 — ``./pfv seed``'s own dataset, on the branch that used to
    orphan a day.

    ``seed.py`` posts three closed periods and one open one, then PUTs cycle
    day 25. With ``today.day < 25`` the old handler re-rooted the open
    period from the 24th to the 25th of the previous month, orphaning the
    24th: a day belonging to no period, invisible to ``list_budgets`` and
    the forecast while still counting toward the account balance.

    The date is FROZEN because the real clock spends most of the month in
    the ``>= 25`` branch, where the old code happened to be a no-op. Seed's
    own dates are deliberately NOT patched — the delete fixes this for
    free, and patching seed.py would mask the regression.
    """
    seed_today = datetime.date(2026, 3, 10)
    assert seed_today.day < 25, "this case only bites in the `< 25` branch"
    _freeze_today(monkeypatch, seed_today)

    ids = await _seed(session_factory, cycle_day=1)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    # The REAL planner, not a hand-copy (TBD-345). This block used to restate
    # all six boundary expressions and the `last_end` fallback inline, so a
    # geometry change in seed.py left this test happily validating the old
    # shape under a comment claiming it tracked the new one.
    # `seed.plan_billing_periods` is pure and importable, so the coupling can
    # be real rather than asserted in prose.
    closed_periods, current_start = seed.plan_billing_periods(seed_today)
    for start, end in closed_periods:
        resp = client.post(
            "/api/v1/settings/billing-period",
            json={"start_date": start.isoformat(), "end_date": end.isoformat()},
        )
        assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/v1/settings/billing-period",
        json={"start_date": current_start.isoformat()},
    )
    assert resp.status_code == 200, resp.text

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": 25}
    )
    assert resp.status_code == 200, resp.text

    periods = await _periods(session_factory, ids["org"])
    assert [p.start_date for p in periods] == [
        closed_periods[0][0], closed_periods[1][0], current_start,
    ]
    # No gap: every closed period ends the day before the next one starts.
    for earlier, later in zip(periods, periods[1:]):
        assert earlier.end_date is not None
        assert earlier.end_date + datetime.timedelta(days=1) == later.start_date, (
            f"gap or overlap between {earlier.start_date}..{earlier.end_date} "
            f"and {later.start_date}"
        )
    assert periods[-1].start_date == current_start
    assert periods[-1].end_date is None


@pytest.mark.asyncio
async def test_update_billing_cycle_on_an_org_with_no_open_period(
    session_factory, monkeypatch
):
    """TBD-239 review F5 — the one org shape where this endpoint DOES write
    a period row.

    The spec's "mutates zero ``billing_periods`` rows" holds only for an org
    that already has an open period. With none, the retained
    ``get_current_period`` call takes its auto-create branch and commits,
    carrying the pending ``billing_cycle_day`` with it. Pinned here so the
    behaviour is a decision rather than a surprise: the new row lands on the
    NEW grid, because the pending assignment is autoflushed before
    ``get_current_period`` re-reads the org in the same session.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed(session_factory, cycle_day=1)
    assert await _periods(session_factory, ids["org"]) == []
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": 15}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"billing_cycle_day": 15}

    periods = await _periods(session_factory, ids["org"])
    assert len(periods) == 1
    assert periods[0].end_date is None
    # _FROZEN_TODAY is 2026-03-20, so the day-15 window containing it opens
    # on 2026-03-15. The day-1 window would have opened on 2026-03-01.
    assert periods[0].start_date == billing_service.current_cycle_window(
        15, _FROZEN_TODAY
    )[0]

    # The column really was persisted, and the audit row names the row that
    # was just auto-created.
    async with session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.id == ids["org"]))
        ).scalar_one()
    assert org.billing_cycle_day == 15

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert len(rows) == 1
    assert rows[0].detail["period_id"] == periods[0].id
    assert rows[0].detail["open_period_start"] == periods[0].start_date.isoformat()


@pytest.mark.asyncio
async def test_update_billing_cycle_rejects_non_admin(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["member"]))

    resp = client.put("/api/v1/settings/billing-cycle", json={"billing_cycle_day": 9})

    assert resp.status_code == 403
    assert await _audit_rows(session_factory) == []


# ── billing_service.ensure_future_periods ─────────────────────────────────
#
# TBD-239 §2. The intersection predicate runs in SQL over the RAW
# `end_date`. Built with `effective_end` / COALESCE semantics instead, the
# open row (end_date IS NULL) would intersect every candidate and stub
# creation would stop for every org, silently breaking next-period budgets.
# The first test below is the guard for exactly that.


@pytest.mark.asyncio
async def test_ensure_future_periods_still_creates_stubs_on_a_healthy_org(
    session_factory, monkeypatch
):
    """Case 4 — THE guard for §2.

    A wrong intersection predicate does not fail loudly; it just creates
    nothing. Without this test the first symptom is a budgets test failing
    somewhere else entirely.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed(session_factory, cycle_day=15)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 2, 15))

    async with session_factory() as db:
        created = await billing_service.ensure_future_periods(
            db, ids["org"], count=3
        )

    assert [(p.start_date, p.end_date) for p in created] == [
        (datetime.date(2026, 3, 15), datetime.date(2026, 4, 14)),
        (datetime.date(2026, 4, 15), datetime.date(2026, 5, 14)),
        (datetime.date(2026, 5, 15), datetime.date(2026, 6, 14)),
    ]
    assert _intersecting_pairs(await _periods(session_factory, ids["org"])) == []


@pytest.mark.asyncio
async def test_ensure_future_periods_is_idempotent(session_factory, monkeypatch):
    """The exact-start skip the intersection test replaces still holds: a
    second call with the same cycle day adds nothing."""
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed(session_factory, cycle_day=15)
    await _add_period(session_factory, ids["org"], datetime.date(2026, 2, 15))

    async with session_factory() as db:
        await billing_service.ensure_future_periods(db, ids["org"], count=3)
    async with session_factory() as db:
        again = await billing_service.ensure_future_periods(db, ids["org"], count=3)

    assert again == []
    assert len(await _periods(session_factory, ids["org"])) == 4


@pytest.mark.asyncio
async def test_ensure_future_periods_builds_no_second_grid_after_cycle_change(
    session_factory, monkeypatch
):
    """Case 5 — the dual grid, which the §1 delete alone does NOT fix.

    An off-grid org already carries stubs on the old cycle-day grid. After
    the admin changes the cycle day, the next Budgets or Forecasts mount
    calls `ensure_future_periods` with `base = current.start_date` and the
    NEW cycle day, so every candidate lands mid-stub. Exact-start matching
    missed all of them and built a second, overlapping grid.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed_off_grid(session_factory)
    # Stubs as a prior run on cycle day 15 would have left them.
    for start, end in [
        (datetime.date(2026, 4, 15), datetime.date(2026, 5, 14)),
        (datetime.date(2026, 5, 15), datetime.date(2026, 6, 14)),
        (datetime.date(2026, 6, 15), datetime.date(2026, 7, 14)),
    ]:
        await _add_period(session_factory, ids["org"], start, end)
    before = await _roster(session_factory, ids["org"])

    client = TestClient(_make_app(session_factory, ids["owner"]))
    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": 25}
    )
    assert resp.status_code == 200, resp.text

    async with session_factory() as db:
        created = await billing_service.ensure_future_periods(
            db, ids["org"], count=3
        )

    # Candidates were 2026-04-25, 2026-05-25 and 2026-06-25 — each one
    # strictly inside an existing stub.
    assert created == []
    assert await _roster(session_factory, ids["org"]) == before
    assert _intersecting_pairs(await _periods(session_factory, ids["org"])) == []


@pytest.mark.asyncio
async def test_ensure_future_periods_on_a_stale_open_period(
    session_factory, monkeypatch
):
    """Case 6 — an org whose open period started four months ago.

    `base` is the open period's start, not today (see the docstring fix in
    §2), so the candidates are historic. They must still be created: the
    open row's NULL `end_date` carries no extent and cannot intersect
    anything, and no candidate can reach backwards into it because every
    candidate lands in a strictly later calendar month.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed(session_factory, cycle_day=10)
    open_start = datetime.date(2025, 11, 10)
    await _add_period(session_factory, ids["org"], open_start)

    async with session_factory() as db:
        created = await billing_service.ensure_future_periods(
            db, ids["org"], count=3
        )

    assert [(p.start_date, p.end_date) for p in created] == [
        (datetime.date(2025, 12, 10), datetime.date(2026, 1, 9)),
        (datetime.date(2026, 1, 10), datetime.date(2026, 2, 9)),
        (datetime.date(2026, 2, 10), datetime.date(2026, 3, 9)),
    ]
    assert all(p.start_date > open_start for p in created)
    assert _intersecting_pairs(await _periods(session_factory, ids["org"])) == []


@pytest.mark.asyncio
async def test_ensure_future_periods_survives_a_concurrent_stub_revival(
    session_factory, monkeypatch
):
    """TBD-239 review F1 — the exact-start arm is not decoration.

    ``close_period`` does not INSERT when a stub already sits at its
    ``new_start``; it REVIVES that stub (``existing.end_date = None``). So a
    row that the window-intersection arm matched a moment ago can turn into
    an open row at a start this loop is still about to propose. With only
    the intersection arm the candidate check then misses it and ``db.add``
    inserts a duplicate. The unique-constraint violation does NOT surface at
    the ``db.commit()`` that the ``except IntegrityError`` below it guards —
    it surfaces from autoflush inside the NEXT iteration's ``db.scalar``,
    outside that try, and escapes as an unhandled 500 through two page
    mounts and three budget services.
    """
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed(session_factory, cycle_day=25)
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 1, 25), datetime.date(2026, 2, 24),
    )
    open_id = await _add_period(
        session_factory, ids["org"], datetime.date(2026, 2, 25)
    )
    revived_id = await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 3, 25), datetime.date(2026, 4, 24),
    )
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 4, 25), datetime.date(2026, 5, 24),
    )
    await _add_period(
        session_factory, ids["org"],
        datetime.date(2026, 5, 25), datetime.date(2026, 6, 24),
    )

    # `BillingCloseJob` ticks (900s, `automate_billing_close` defaults on)
    # and revives the stub at the boundary: [Mar 25, Apr 24] -> [Mar 25, NULL].
    async with session_factory() as db:
        row = (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.id == revived_id)
            )
        ).scalar_one()
        row.end_date = None
        await db.commit()

    # Pin `base` to the PRE-tick open period, which is what the in-flight
    # caller read. Re-reading it now would return the revived row instead
    # (`get_current_period` orders `start_date DESC`) and hide the race.
    # `today` mirrors the real signature (TBD-297); unused here.
    async def _stale_current(db, org_id, *, today=None):
        return (
            await db.execute(
                select(BillingPeriod).where(BillingPeriod.id == open_id)
            )
        ).scalar_one()

    monkeypatch.setattr(billing_service, "get_current_period", _stale_current)

    async with session_factory() as db:
        created = await billing_service.ensure_future_periods(
            db, ids["org"], count=3
        )

    # Mar 25 is skipped by the exact-start arm despite now being open;
    # Apr 25 and May 25 by the intersection arm.
    assert created == []
    starts = [p.start_date for p in await _periods(session_factory, ids["org"])]
    assert starts == [
        datetime.date(2026, 1, 25),
        datetime.date(2026, 2, 25),
        datetime.date(2026, 3, 25),
        datetime.date(2026, 4, 25),
        datetime.date(2026, 5, 25),
    ]
    assert len(starts) == len(set(starts))


@pytest.mark.asyncio
async def test_ensure_future_periods_logs_the_skip(session_factory, monkeypatch):
    """The skip is silent to the caller by design, so the structlog warning
    is the only trace an off-grid org leaves. Asserted so the event name and
    its payload keys stay greppable in production."""
    _freeze_today(monkeypatch, _FROZEN_TODAY)
    ids = await _seed_off_grid(session_factory)
    for start, end in [
        (datetime.date(2026, 4, 15), datetime.date(2026, 5, 14)),
        (datetime.date(2026, 5, 15), datetime.date(2026, 6, 14)),
        (datetime.date(2026, 6, 15), datetime.date(2026, 7, 14)),
    ]:
        await _add_period(session_factory, ids["org"], start, end)

    client = TestClient(_make_app(session_factory, ids["owner"]))
    assert client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": 25}
    ).status_code == 200

    with structlog.testing.capture_logs() as logs:
        async with session_factory() as db:
            created = await billing_service.ensure_future_periods(
                db, ids["org"], count=3
            )

    assert created == []
    skips = [e for e in logs if e.get("event") == "billing.stub.skipped_overlap"]
    assert len(skips) == 3
    assert [e["candidate_start"] for e in skips] == [
        "2026-04-25", "2026-05-25", "2026-06-25",
    ]
    for entry in skips:
        assert entry["log_level"] == "warning"
        assert entry["org_id"] == ids["org"]
        assert entry["existing_period_id"] is not None
        assert entry["candidate_end"] > entry["candidate_start"]


# ── billing_service.reanchor_period_dependents ────────────────────────────


@pytest.mark.asyncio
async def test_reanchor_writes_both_columns_with_non_null_end(session_factory):
    """Every router-level case here passes new_end=None (the open period),
    so without this the "writes period_end too" fix is never observed."""
    ids = await _seed(session_factory)
    old_start = datetime.date(2026, 3, 1)
    new_start = datetime.date(2026, 3, 5)
    new_end = datetime.date(2026, 4, 4)
    await _add_budget(session_factory, ids["org"], ids["groceries"], old_start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=old_start,
            new_start=new_start, new_end=new_end,
        )
        await db.commit()

    assert moved == 1
    budgets = await _budgets(session_factory, ids["org"])
    assert budgets[0].period_start == new_start
    assert budgets[0].period_end == new_end


@pytest.mark.asyncio
async def test_reanchor_identity_with_changed_end_updates_end_only(session_factory):
    ids = await _seed(session_factory)
    start = datetime.date(2026, 3, 1)
    new_end = datetime.date(2026, 3, 31)
    await _add_budget(session_factory, ids["org"], ids["groceries"], start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=start,
            new_start=start, new_end=new_end,
        )
        await db.commit()

    assert moved == 1
    budgets = await _budgets(session_factory, ids["org"])
    assert budgets[0].period_start == start
    assert budgets[0].period_end == new_end


@pytest.mark.asyncio
async def test_reanchor_identity_counts_only_stale_rows(session_factory):
    """In the identity fall-through the UPDATE must be scoped to the rows
    whose `period_end` snapshot is actually stale. Unscoped it matches every
    row at `old_start` and `budgets_reanchored` over-reports."""
    ids = await _seed(session_factory)
    start = datetime.date(2026, 3, 1)
    new_end = datetime.date(2026, 3, 31)
    # Already correct — must not be counted.
    await _add_budget(session_factory, ids["org"], ids["groceries"], start, new_end)
    # Stale snapshot — the only genuine move.
    await _add_budget(session_factory, ids["org"], ids["transport"], start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=start,
            new_start=start, new_end=new_end,
        )
        await db.commit()

    assert moved == 1
    budgets = await _budgets(session_factory, ids["org"])
    assert {b.period_end for b in budgets} == {new_end}


@pytest.mark.asyncio
async def test_reanchor_identity_with_unchanged_end_returns_zero(session_factory):
    ids = await _seed(session_factory)
    start = datetime.date(2026, 3, 1)
    await _add_budget(session_factory, ids["org"], ids["groceries"], start)
    await _add_budget(session_factory, ids["org"], ids["transport"], start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=start,
            new_start=start, new_end=None,
        )
        await db.commit()

    assert moved == 0


@pytest.mark.asyncio
async def test_reanchor_counts_rows_and_is_org_scoped(session_factory):
    ids = await _seed(session_factory)
    other = await _seed_other_org(session_factory)
    old_start = datetime.date(2026, 3, 1)
    new_start = datetime.date(2026, 3, 5)
    await _add_budget(session_factory, ids["org"], ids["groceries"], old_start)
    await _add_budget(session_factory, ids["org"], ids["transport"], old_start)
    await _add_budget(session_factory, other["org"], other["category"], old_start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=old_start,
            new_start=new_start, new_end=None,
        )
        await db.commit()

    assert moved == 2
    async with session_factory() as db:
        foreign = (
            await db.execute(
                select(Budget).where(Budget.org_id == other["org"])
            )
        ).scalar_one()
    assert foreign.period_start == old_start


@pytest.mark.asyncio
async def test_reanchor_conflict_leaves_budgets_untouched(session_factory):
    ids = await _seed(session_factory)
    old_start = datetime.date(2026, 3, 1)
    new_start = datetime.date(2026, 3, 5)
    await _add_budget(session_factory, ids["org"], ids["groceries"], old_start)
    await _add_budget(session_factory, ids["org"], ids["transport"], old_start)
    await _add_budget(session_factory, ids["org"], ids["groceries"], new_start)

    async with session_factory() as db:
        with pytest.raises(ConflictError) as exc:
            await billing_service.reanchor_period_dependents(
                db, org_id=ids["org"], old_start=old_start,
                new_start=new_start, new_end=None,
            )
    assert exc.value.code == "budget_period_conflict"
    assert "Groceries" in exc.value.detail

    budgets = await _budgets(session_factory, ids["org"])
    assert sorted(b.period_start for b in budgets) == [
        old_start, old_start, new_start,
    ]


@pytest.mark.asyncio
async def test_reanchor_ignores_other_categories_at_destination(session_factory):
    """A budget already sitting at the destination for a category that is
    NOT being moved is not a conflict — uq_budget_org_cat_period is per
    (org, category, period_start)."""
    ids = await _seed(session_factory)
    old_start = datetime.date(2026, 3, 1)
    new_start = datetime.date(2026, 3, 5)
    await _add_budget(session_factory, ids["org"], ids["groceries"], old_start)
    await _add_budget(session_factory, ids["org"], ids["transport"], new_start)

    async with session_factory() as db:
        moved = await billing_service.reanchor_period_dependents(
            db, org_id=ids["org"], old_start=old_start,
            new_start=new_start, new_end=None,
        )
        await db.commit()

    assert moved == 1
    budgets = await _budgets(session_factory, ids["org"])
    assert {(b.category_id, b.period_start) for b in budgets} == {
        (ids["groceries"], new_start),
        (ids["transport"], new_start),
    }


async def _seed_other_org(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Other", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        cat = Category(org_id=org.id, name="Groceries", slug="groceries")
        db.add(cat)
        await db.commit()
        return {"org": org.id, "category": cat.id}
