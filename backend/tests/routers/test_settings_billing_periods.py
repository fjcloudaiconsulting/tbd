"""Route-level coverage for the billing-period endpoints in
``routers/settings.py`` (TBD-232, slice 1 of the TBD-213 split).

Before this file the router had **zero** billing coverage: three write
paths (``PUT /billing-cycle``, ``POST /billing-period``,
``POST /billing-period/close``) that mutate period boundaries and budget
anchors were exercised only indirectly through the service layer.

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
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import Category
from app.models.user import Organization, Role, User
from app.routers.settings import router as settings_router
from app.security import hash_password
from app.services import billing_service
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


def _anchor_for(cycle_day: int, today: datetime.date) -> datetime.date:
    """Mirror the router's anchor math (settings.py) so tests can name the
    destination start without hardcoding a wall-clock date.

    See reference_wall_clock_date_bomb_tests: never pin a literal near-today
    date, derive it.
    """
    if today.day >= cycle_day:
        return datetime.date(today.year, today.month, cycle_day)
    prev = datetime.date(today.year, today.month, 1) - datetime.timedelta(days=1)
    return datetime.date(prev.year, prev.month, cycle_day)


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

    rows = await _audit_rows(session_factory, "org.billing_period.closed")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.target_org_id == ids["org"]
    assert row.actor_email == "owner@acme.io"
    assert row.detail["closed_period_id"] == period_id
    assert row.detail["closed_period_start"] == start.isoformat()
    # Resolved from the NEW period's start, never re-derived in the router.
    assert row.detail["close_date"] == (
        new_start - datetime.timedelta(days=1)
    ).isoformat()
    assert row.detail["new_period_start"] == new_start.isoformat()


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


# ── PUT /billing-cycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_billing_cycle_writes_audit_row(session_factory):
    ids = await _seed(session_factory, cycle_day=1)
    today = datetime.date.today()
    new_day = 15 if today.day != 15 else 14
    old_start = _anchor_for(1, today)
    period_id = await _add_period(session_factory, ids["org"], old_start)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": new_day}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"billing_cycle_day": new_day}

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome.value == "success"
    assert row.detail["old_day"] == 1
    assert row.detail["new_day"] == new_day
    assert row.detail["period_id"] == period_id
    assert row.detail["old_start"] == old_start.isoformat()
    assert row.detail["new_start"] == _anchor_for(new_day, today).isoformat()
    assert row.detail["budgets_reanchored"] == 0


@pytest.mark.asyncio
async def test_update_billing_cycle_reanchors_budget_start_and_end(session_factory):
    ids = await _seed(session_factory, cycle_day=1)
    today = datetime.date.today()
    new_day = 15 if today.day != 15 else 14
    old_start = _anchor_for(1, today)
    new_start = _anchor_for(new_day, today)
    assert old_start != new_start

    await _add_period(session_factory, ids["org"], old_start)
    # A stale non-null snapshot, exactly the shape close_period can revive.
    await _add_budget(
        session_factory, ids["org"], ids["groceries"], old_start,
        old_start + datetime.timedelta(days=20),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": new_day}
    )

    assert resp.status_code == 200, resp.text
    budgets = await _budgets(session_factory, ids["org"])
    assert len(budgets) == 1
    assert budgets[0].period_start == new_start
    # The open period's end_date is None, so the snapshot must become None.
    assert budgets[0].period_end is None

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert rows[0].detail["budgets_reanchored"] == 1


@pytest.mark.asyncio
async def test_update_billing_cycle_identity_case_is_a_noop(session_factory):
    """THE test of this slice.

    An org that ever closed manually has an open period starting on an
    arbitrary day. Re-saving the same cycle day (or `./pfv seed`, which
    PUTs cycle day 25 right after creating a period starting on the 25th)
    hits old_start == new_start. A naive pre-flight finds every budget
    conflicting with itself and turns a working no-op into a hard 409.
    """
    ids = await _seed(session_factory, cycle_day=1)
    today = datetime.date.today()
    cycle_day = today.day if today.day <= 28 else 28
    start = _anchor_for(cycle_day, today)
    assert start == datetime.date(today.year, today.month, cycle_day)

    await _add_period(session_factory, ids["org"], start)
    budget_id = await _add_budget(
        session_factory, ids["org"], ids["groceries"], start
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": cycle_day}
    )

    assert resp.status_code == 200, resp.text
    budgets = await _budgets(session_factory, ids["org"])
    assert [b.id for b in budgets] == [budget_id]
    assert budgets[0].period_start == start
    assert budgets[0].period_end is None

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert rows[0].outcome.value == "success"
    assert rows[0].detail["budgets_reanchored"] == 0


@pytest.mark.asyncio
async def test_update_billing_cycle_budget_conflict_returns_409(session_factory):
    ids = await _seed(session_factory, cycle_day=1)
    today = datetime.date.today()
    new_day = 15 if today.day != 15 else 14
    old_start = _anchor_for(1, today)
    new_start = _anchor_for(new_day, today)
    assert old_start != new_start

    await _add_period(session_factory, ids["org"], old_start)
    await _add_budget(session_factory, ids["org"], ids["groceries"], old_start)
    # Same category already budgeted at the destination start.
    await _add_budget(session_factory, ids["org"], ids["groceries"], new_start)
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": new_day}
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "budget_period_conflict"
    assert "Groceries" in body["detail"]

    # Nothing moved, and the cycle day did not stick.
    budgets = await _budgets(session_factory, ids["org"])
    assert sorted(b.period_start for b in budgets) == sorted([old_start, new_start])
    async with session_factory() as db:
        org = (
            await db.execute(
                select(Organization).where(Organization.id == ids["org"])
            )
        ).scalar_one()
    assert org.billing_cycle_day == 1

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail["reason"] == "budget_period_conflict"


@pytest.mark.asyncio
async def test_update_billing_cycle_period_conflict_returns_409(session_factory):
    ids = await _seed(session_factory, cycle_day=1)
    today = datetime.date.today()
    new_day = 15 if today.day != 15 else 14
    old_start = _anchor_for(1, today)
    new_start = _anchor_for(new_day, today)
    assert old_start != new_start

    await _add_period(session_factory, ids["org"], old_start)
    # A closed period already occupies the destination start.
    await _add_period(
        session_factory, ids["org"], new_start,
        new_start + datetime.timedelta(days=5),
    )
    client = TestClient(_make_app(session_factory, ids["owner"]))

    resp = client.put(
        "/api/v1/settings/billing-cycle", json={"billing_cycle_day": new_day}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "billing_period_exists"

    async with session_factory() as db:
        period = (
            await db.execute(
                select(BillingPeriod).where(
                    BillingPeriod.org_id == ids["org"],
                    BillingPeriod.end_date.is_(None),
                )
            )
        ).scalar_one()
    assert period.start_date == old_start

    rows = await _audit_rows(session_factory, "org.billing_cycle_day.updated")
    assert len(rows) == 1
    assert rows[0].outcome.value == "failure"
    assert rows[0].detail["reason"] == "billing_period_exists"


@pytest.mark.asyncio
async def test_update_billing_cycle_rejects_non_admin(session_factory):
    ids = await _seed(session_factory)
    client = TestClient(_make_app(session_factory, ids["member"]))

    resp = client.put("/api/v1/settings/billing-cycle", json={"billing_cycle_day": 9})

    assert resp.status_code == 403
    assert await _audit_rows(session_factory) == []


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
