"""TBD-283 — the frontier lower bound as an HTTP contract.

``tests/services/test_recurring_frontier_lower_bound.py`` fences the RULE with
an injected clock. This file fences the two things only a request can show:

  1. the rejection is a **400** with a readable ``detail``, on every write path
     — including ``promote-to-recurring``, whose rejection MOVED from 422 to
     400 when ``PromoteToRecurringRequest._next_due_date_not_past`` was deleted;
  2. ``PUT {"is_active": true}`` — the ONLY PUT the recurring page issues
     (``frontend/app/recurring/page.tsx`` ``handleResume``) — still returns 200
     on a long-paused template.

⚠ No clock is injectable through a route, so these read ``date.today()`` and
derive the org's ``billing_cycle_day`` from it (``_anchor``). Every date is
``p_start ± n``, never ``today ± n``, so nothing here is a wall-clock bomb.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
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
from app.deps import get_current_user
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.base import Base
from app.models.category import CategoryType
from app.models.recurring import Frequency, RecurringTransaction
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.recurring import router as recurring_router
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services.billing_service import current_cycle_window
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

DAY = timedelta(days=1)


def _anchor(today: date) -> tuple[int, date]:
    """A ``(cycle_day, p_start)`` pair with ``p_start`` STRICTLY behind ``today``.

    Five days back, nudged to a day-of-month that exists in every month so the
    cycle arithmetic is not month-length dependent. Strictness is what makes the
    "past date, still accepted" assertions below distinguishable from
    ``>= today``; it is asserted, not assumed.
    """
    d = today - timedelta(days=5)
    while d.day > 28:
        d -= DAY
    cycle_day = d.day
    p_start, _ = current_cycle_window(cycle_day, today)
    assert p_start == d
    assert p_start < today
    return cycle_day, p_start


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


def make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    @app.exception_handler(NotFoundError)
    async def _nf(_req, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _ve(_req, exc: ValidationError):
        return JSONResponse(status_code=400, content={"detail": exc.detail})

    @app.exception_handler(ConflictError)
    async def _ce(_req, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    app.include_router(recurring_router)
    app.include_router(transactions_router)
    return app


async def _seed(factory, *, cycle_day: int) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=cycle_day)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="root", email="root@example.com",
            password_hash=hash_password("pw-1234567"), role=Role.OWNER,
            is_superadmin=True, is_active=True, email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        acct = Account(
            org_id=org.id, name="Acct", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        cat = Category(
            org_id=org.id, name="Rent", slug="rent",
            type=CategoryType.EXPENSE, is_system=False,
        )
        db.add_all([acct, cat])
        await db.commit()
        return {"org_id": org.id, "account_id": acct.id, "category_id": cat.id}


async def _add_template(
    factory, seed: dict, *, due: date, is_active: bool = True,
) -> int:
    async with factory() as db:
        t = RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["category_id"], description="Rent",
            amount=Decimal("500"), type="expense", frequency=Frequency.MONTHLY,
            next_due_date=due, auto_settle=False, is_active=is_active,
        )
        db.add(t)
        await db.commit()
        return t.id


async def _add_tx(factory, seed: dict, *, on: date) -> int:
    async with factory() as db:
        tx = Transaction(
            org_id=seed["org_id"], account_id=seed["account_id"],
            category_id=seed["category_id"], description="Coffee",
            amount=Decimal("12.50"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=on, settled_date=on,
        )
        db.add(tx)
        await db.commit()
        return tx.id


def _body(seed: dict, due: date) -> dict:
    return {
        "account_id": seed["account_id"],
        "category_id": seed["category_id"],
        "description": "Rent",
        "amount": "500",
        "type": "expense",
        "frequency": "monthly",
        "next_due_date": due.isoformat(),
    }


# ── POST /recurring ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_before_cycle_start_is_400_and_names_the_date(session_factory):
    """FENCE. Domain 400 (not 422), and the boundary is IN the message.

    Wrong implementation killed: putting the bound back in a pydantic
    validator. That surfaces as a 422 with pydantic's own envelope and no
    ``detail`` string the user could act on — and, being clock-based rather
    than cycle-based, it would report the wrong boundary anyway.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/recurring", json=_body(seed, p_start - DAY))
    assert res.status_code == 400
    assert p_start.isoformat() in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_on_a_past_date_inside_the_open_cycle_is_201(session_factory):
    """FENCE. ``p_start`` is five days behind ``today`` (asserted in ``_anchor``).

    Wrong implementation killed: any ``>= today`` bound at any layer — it 4xx's
    this legitimate mid-cycle anchor.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post("/api/v1/recurring", json=_body(seed, p_start))
    assert res.status_code == 201, res.text
    assert res.json()["next_due_date"] == p_start.isoformat()


# ── PUT /recurring/{id} ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_only_put_on_a_stale_template_is_200(session_factory):
    """FENCE — condition 1, over the wire.

    ``handleResume`` sends ``{"is_active": true}`` and nothing else. The one PUT
    the product actually issues must not start 4xx-ing because of a bound the
    request never mentions.

    ⚠ Honest scope — the same caveat the service-side F4a carries, stated here
    because this test otherwise reads as stronger coverage than it is. Since
    TBD-300's re-anchor landed, this 200 does NOT on its own prove the gate
    exists: an UNGATED check placed after the re-anchor passes here too,
    because by then the re-anchor has already walked the 300-day-stale frontier
    onto ``p_start``. The state was rescued, not left alone. What kills "drop
    the gate" is the service-side F4b/F4c, whose bodies make no re-anchor
    eligible to do the rescuing; nothing over the wire kills it, because no
    route test here sends a body that leaves a stale frontier stale.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)
    tid = await _add_template(
        session_factory, seed, due=p_start - timedelta(days=300), is_active=False
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(f"/api/v1/recurring/{tid}", json={"is_active": True})
    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is True


@pytest.mark.asyncio
async def test_frequency_only_put_on_a_stale_template_is_400(session_factory):
    """FENCE — condition 2, over the wire. The ``frequency`` clause of the gate.

    Wrong implementation killed: gating the check on ``next_due_date`` alone.
    This request carries no date at all, and it is the request that multiplies
    the stale template's implied occurrences.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)
    tid = await _add_template(
        session_factory, seed, due=p_start - timedelta(days=300), is_active=True
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(f"/api/v1/recurring/{tid}", json={"frequency": "weekly"})
    assert res.status_code == 400
    assert p_start.isoformat() in res.json()["detail"]


# ── POST /transactions/{id}/promote-to-recurring ───────────────────────────

@pytest.mark.asyncio
async def test_promote_past_date_inside_the_cycle_is_201(session_factory):
    """FENCE — the deliberate RELAXATION, over the wire.

    ``p_start`` is in the past. Before TBD-283 this was a 422 from
    ``PromoteToRecurringRequest._next_due_date_not_past``. Goes red if that
    validator survives anywhere, or if the service kept its own ``< today``
    comparison.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)
    tx_id = await _add_tx(session_factory, seed, on=p_start)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": p_start.isoformat()},
        )
    assert res.status_code == 201, res.text
    assert res.json()["recurring_id"] is not None


@pytest.mark.asyncio
async def test_promote_before_cycle_start_is_400_not_422(session_factory):
    """FENCE — the status-code MOVE, stated as an assertion.

    422 was the schema validator's code; 400 is the domain error's. Asserting
    ``!= 422`` explicitly is the point: a test that only checked "not 2xx"
    would pass with the validator still in place, and the unification would be
    unfenced.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)
    tx_id = await _add_tx(session_factory, seed, on=p_start)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={
                "frequency": "monthly",
                "next_due_date": (p_start - DAY).isoformat(),
            },
        )
    assert res.status_code == 400
    assert p_start.isoformat() in res.json()["detail"]
