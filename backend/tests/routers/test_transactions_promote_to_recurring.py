"""Router-level tests for POST /api/v1/transactions/{id}/promote-to-recurring.

Service-level invariants are covered by
tests/services/test_transaction_service_promote_to_recurring.py.

These tests focus on:
  - HTTP body validation (extra=forbid, frequency enum). ⚠ NOT a past-date 422:
    TBD-283 deleted ``PromoteToRecurringRequest._next_due_date_not_past``, so
    the only lower bound left is the org's current billing cycle start, checked
    in the service layer and surfaced as a 400 (see
    ``test_promote_to_recurring_past_date_is_no_longer_a_schema_422``).
  - status mapping for service-domain exceptions (NotFoundError → 404,
    ValidationError → 400)
  - happy-path response shape (TransactionResponse with recurring_id set)
  - the new RecurringTransaction is queryable via GET /api/v1/recurring
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
from sqlalchemy import event
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


# ── fixtures ───────────────────────────────────────────────────────────────


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
        from sqlalchemy import select as _select
        async with session_factory() as db:
            return (
                await db.execute(_select(User).where(User.is_superadmin.is_(True)))
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

    app.include_router(transactions_router)
    app.include_router(recurring_router)
    return app


def _anchor(today: date) -> tuple[int, date]:
    """A ``(cycle_day, p_start)`` pair with ``p_start`` STRICTLY behind ``today``.

    ⚠ The frontier-bound test below cannot use the default
    ``billing_cycle_day=1`` seed. On the 1st of the month ``p_start == today``,
    so its ACCEPT arm is satisfied by the deleted ``>= today`` rule too and the
    relaxation goes unfenced for that one day. Five days back, nudged onto a
    day-of-month that exists in every month so the cycle arithmetic is not
    month-length dependent; the strictness is ASSERTED, not assumed. Mirrors
    ``_anchor`` in ``test_recurring_frontier_lower_bound_route.py``.
    """
    d = today - timedelta(days=5)
    while d.day > 28:
        d -= timedelta(days=1)
    cycle_day = d.day
    p_start, _ = current_cycle_window(cycle_day, today)
    assert p_start == d
    assert p_start < today
    return cycle_day, p_start


async def _seed(factory, *, cycle_day: int = 1) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=cycle_day)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="root",
            email="root@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        at = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        db.add_all([user, at])
        await db.flush()
        a1 = Account(
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=Decimal("1000"), currency="EUR",
        )
        a2 = Account(
            org_id=org.id, name="Acct B", account_type_id=at.id,
            balance=Decimal("0"), currency="EUR",
        )
        db.add_all([a1, a2])
        await db.flush()
        cat_groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        cat_transfer = Category(
            org_id=org.id, name="Transfer", slug="transfer",
            type=CategoryType.BOTH, is_system=True,
        )
        db.add_all([cat_groceries, cat_transfer])
        await db.commit()
        return {
            "org_id": org.id,
            "a1_id": a1.id,
            "a2_id": a2.id,
            "cat_groceries_id": cat_groceries.id,
            "cat_transfer_id": cat_transfer.id,
        }


async def _add_tx(
    factory,
    *,
    org_id: int,
    account_id: int,
    category_id: int,
    type: TransactionType = TransactionType.EXPENSE,
    amount: Decimal = Decimal("12.50"),
    description: str = "Coffee",
    on_date: date | None = None,
    linked_transaction_id: int | None = None,
    recurring_id: int | None = None,
) -> int:
    async with factory() as db:
        on_date = on_date or date(2026, 5, 1)
        tx = Transaction(
            org_id=org_id,
            account_id=account_id,
            category_id=category_id,
            description=description,
            amount=amount,
            type=type,
            status=TransactionStatus.SETTLED,
            date=on_date,
            settled_date=on_date,
            linked_transaction_id=linked_transaction_id,
            recurring_id=recurring_id,
            is_imported=False,
        )
        db.add(tx)
        await db.commit()
        return tx.id


def _future_date(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ── happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_recurring_happy_path(session_factory):
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
        amount=Decimal("12.50"), description="Weekly coffee",
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == tx_id
    assert body["recurring_id"] is not None
    new_rec_id = body["recurring_id"]

    # The new RecurringTransaction is reachable via GET /api/v1/recurring.
    with TestClient(app) as client:
        rec_res = client.get("/api/v1/recurring")
    assert rec_res.status_code == 200, rec_res.text
    rec_rows = rec_res.json()
    rec_match = next((r for r in rec_rows if r["id"] == new_rec_id), None)
    assert rec_match is not None
    assert rec_match["account_id"] == seed["a1_id"]
    assert rec_match["category_id"] == seed["cat_groceries_id"]
    assert rec_match["description"] == "Weekly coffee"
    assert rec_match["frequency"] == "monthly"
    assert rec_match["is_active"] is True
    assert rec_match["auto_settle"] is False


# ── 422: bad body shape ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_recurring_rejects_extra_fields(session_factory):
    """``auto_settle`` is now an accepted field (frontend create-with-repeat
    forwards the user's toggle). Anything else still gets rejected by
    `model_config = ConfigDict(extra="forbid")`."""
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={
                "frequency": "monthly",
                "next_due_date": _future_date(),
                "is_active": False,  # not part of the schema
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_promote_to_recurring_rejects_unknown_frequency(session_factory):
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "daily", "next_due_date": _future_date()},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_promote_to_recurring_past_date_is_no_longer_a_schema_422(session_factory):
    """TBD-283 deleted ``PromoteToRecurringRequest._next_due_date_not_past``.

    This test previously asserted 422 for ``today - 1 day``. That validator was
    the strictest of three disagreeing rules on one column and could not see
    ``org.billing_cycle_day``, so it was removed rather than left to pre-empt
    the real bound. Both arms are asserted together: a date inside the open
    cycle is now accepted, and one before it is refused as a domain 400 — never
    a 422.

    ⚠ The dates are derived from ``current_cycle_window``, not from
    ``date.today() - 1``. With ``billing_cycle_day=1``, "yesterday" falls on the
    far side of the boundary on exactly one day per month, and a literal would
    have made this a once-a-month red.

    ⚠ And the cycle day comes from ``_anchor``, not from the seed's default of
    1, for the mirror-image reason: with cycle day 1 the ACCEPT arm sends
    ``p_start == today`` on the 1st of the month, which the deleted ``>= today``
    validator would also have accepted. ``_anchor`` asserts ``p_start < today``,
    so the accepted date is genuinely in the past on every day of the month.
    """
    today = date.today()
    cycle_day, p_start = _anchor(today)
    seed = await _seed(session_factory, cycle_day=cycle_day)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )
    tx_id_2 = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": p_start.isoformat()},
        )
        refused = client.post(
            f"/api/v1/transactions/{tx_id_2}/promote-to-recurring",
            json={
                "frequency": "monthly",
                "next_due_date": (p_start - timedelta(days=1)).isoformat(),
            },
        )
    assert accepted.status_code == 201, accepted.text
    # 400 (domain), not 422 (schema) — the status code moved, deliberately.
    assert refused.status_code == 400, refused.text


# ── 404: not found ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_recurring_not_found(session_factory):
    await _seed(session_factory)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/transactions/99999/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert res.status_code == 404


# ── 400: domain rejections ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_recurring_rejects_transfer_leg(session_factory):
    seed = await _seed(session_factory)
    # Build a paired transfer directly via SQL setup.
    async with session_factory() as db:
        from sqlalchemy import select as _select
        expense_leg = Transaction(
            org_id=seed["org_id"], account_id=seed["a1_id"],
            category_id=seed["cat_transfer_id"], description="xfer out",
            amount=Decimal("50"), type=TransactionType.EXPENSE,
            status=TransactionStatus.SETTLED, date=date(2026, 5, 1),
            settled_date=date(2026, 5, 1),
        )
        income_leg = Transaction(
            org_id=seed["org_id"], account_id=seed["a2_id"],
            category_id=seed["cat_transfer_id"], description="xfer in",
            amount=Decimal("50"), type=TransactionType.INCOME,
            status=TransactionStatus.SETTLED, date=date(2026, 5, 1),
            settled_date=date(2026, 5, 1),
        )
        db.add_all([expense_leg, income_leg])
        await db.flush()
        expense_leg.linked_transaction_id = income_leg.id
        income_leg.linked_transaction_id = expense_leg.id
        await db.commit()
        leg_id = expense_leg.id

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{leg_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert res.status_code == 400
    assert "transfer" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_promote_to_recurring_rejects_already_promoted(session_factory):
    seed = await _seed(session_factory)
    # Pre-create a template, link a tx to it.
    async with session_factory() as db:
        tmpl = RecurringTransaction(
            org_id=seed["org_id"], account_id=seed["a1_id"],
            category_id=seed["cat_groceries_id"], description="x",
            amount=Decimal("5"), type="expense", frequency=Frequency.MONTHLY,
            next_due_date=date.today(), auto_settle=False, is_active=True,
        )
        db.add(tmpl)
        await db.flush()
        tmpl_id = tmpl.id
        await db.commit()
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], recurring_id=tmpl_id,
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert res.status_code == 400
    assert "already" in res.json()["detail"].lower()


# ── round-trip: promote → edit → re-promote rejected ─────────────────────


@pytest.mark.asyncio
async def test_promote_then_edit_then_repromote_rejected(session_factory):
    """Reproduces the punch-list ITEM 1 bug from the API side.

    Scenario:
      1. Caller has an existing tx
      2. POST /promote-to-recurring → tx.recurring_id is set, template A created
      3. PUT /transactions/{id} (edit description) → recurring_id MUST persist
      4. POST /promote-to-recurring again → MUST 400 (no template B)

    Pre-fix the frontend create flow side-stepped this guard by creating an
    independent /recurring template on the create form (never linking the
    source tx via recurring_id). When the user later edited and re-toggled
    "Make recurring", the row's recurring_id was still NULL so promote
    succeeded — yielding two templates. This API-level test pins the
    backend invariant; the create-flow fix lives in the frontend.
    """
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
        amount=Decimal("12.50"), description="Initial",
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        # Step 1: promote
        first = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
        assert first.status_code == 201, first.text
        first_template_id = first.json()["recurring_id"]
        assert first_template_id is not None

        # Step 2: edit description (a routine PUT). recurring_id must NOT
        # be cleared by update_transaction.
        edited = client.put(
            f"/api/v1/transactions/{tx_id}",
            json={"description": "Renamed"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["recurring_id"] == first_template_id

        # Step 3: re-promote MUST 400
        second = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert second.status_code == 400
    assert "already" in second.json()["detail"].lower()

    # And only ONE template exists for this tx.
    async with session_factory() as db:
        from sqlalchemy import select as _select
        rows = (
            await db.execute(
                _select(RecurringTransaction).where(
                    RecurringTransaction.org_id == seed["org_id"]
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_template_id


# ── auto_settle pass-through (create-with-recurring frontend fix) ─────────


@pytest.mark.asyncio
async def test_promote_to_recurring_passes_auto_settle_true(session_factory):
    """Frontend create-with-recurring sends auto_settle so the user's
    "Auto-settle" toggle on the create form survives the round-trip."""
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={
                "frequency": "monthly",
                "next_due_date": _future_date(),
                "auto_settle": True,
            },
        )
    assert res.status_code == 201, res.text
    rec_id = res.json()["recurring_id"]
    assert rec_id is not None

    async with session_factory() as db:
        from sqlalchemy import select as _select
        rec = await db.scalar(
            _select(RecurringTransaction).where(RecurringTransaction.id == rec_id)
        )
    assert rec is not None
    assert rec.auto_settle is True


@pytest.mark.asyncio
async def test_promote_to_recurring_auto_settle_defaults_false(session_factory):
    """Omitting auto_settle keeps the historical default (matches the edit
    flow's promote which doesn't surface the toggle)."""
    seed = await _seed(session_factory)
    tx_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"],
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{tx_id}/promote-to-recurring",
            json={"frequency": "monthly", "next_due_date": _future_date()},
        )
    assert res.status_code == 201, res.text
    rec_id = res.json()["recurring_id"]

    async with session_factory() as db:
        from sqlalchemy import select as _select
        rec = await db.scalar(
            _select(RecurringTransaction).where(RecurringTransaction.id == rec_id)
        )
    assert rec is not None
    assert rec.auto_settle is False
