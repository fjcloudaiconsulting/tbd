"""Router-level tests for the pair / convert / unpair / transfer-candidates
endpoints (Op-1/2/3/4 from the transfers-between-accounts plan).

Service-level invariants are covered in tests/services/test_transaction_service_pair.py.
These tests focus on:
  - schema-level body validation (extra=forbid, required fields),
  - HTTP status mapping for service-domain exceptions,
  - happy-path response shape (sorted-by-id pair, confidence labels).
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
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.transactions import router as transactions_router
from app.security import hash_password
from app.services.exceptions import ConflictError, NotFoundError, ValidationError


# ── fixtures ────────────────────────────────────────────────────────────────


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
    """Build a minimal FastAPI app wired to an isolated SQLite session and a
    static superadmin user. Registers the same domain-exception handlers the
    real app does so 400/404/409 mappings are observable in tests.
    """
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
    return app


async def _seed_base(factory) -> dict:
    """Seed an org, user, two same-currency accounts, and a couple of
    fallback categories. Returns a dict of ids the per-test helpers extend.
    """
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
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
        a3 = Account(
            org_id=org.id, name="Acct C USD", account_type_id=at.id,
            balance=Decimal("0"), currency="USD",
        )
        db.add_all([a1, a2, a3])
        await db.flush()
        cat_groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        cat_salary = Category(
            org_id=org.id, name="Salary", slug="salary",
            type=CategoryType.INCOME, is_system=False,
        )
        # Pre-seed the system Transfer category so recategorize doesn't
        # need to create one mid-test (deterministic ID for assertions).
        cat_transfer = Category(
            org_id=org.id, name="Transfer", slug="transfer",
            type=CategoryType.BOTH, is_system=True,
        )
        db.add_all([cat_groceries, cat_salary, cat_transfer])
        await db.commit()
        return {
            "org_id": org.id,
            "user_id": user.id,
            "a1_id": a1.id,
            "a2_id": a2.id,
            "a3_id": a3.id,
            "cat_groceries_id": cat_groceries.id,
            "cat_salary_id": cat_salary.id,
            "cat_transfer_id": cat_transfer.id,
        }


async def _add_tx(
    factory,
    *,
    org_id: int,
    account_id: int,
    category_id: int,
    type: TransactionType,
    amount: Decimal,
    description: str = "row",
    on_date: date | None = None,
    status: TransactionStatus = TransactionStatus.SETTLED,
    linked_transaction_id: int | None = None,
) -> int:
    async with factory() as db:
        tx = Transaction(
            org_id=org_id,
            account_id=account_id,
            category_id=category_id,
            description=description,
            amount=amount,
            type=type,
            status=status,
            date=on_date or date(2026, 5, 1),
            linked_transaction_id=linked_transaction_id,
            is_imported=False,
        )
        db.add(tx)
        await db.commit()
        return tx.id


# ── 1. POST /pair happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_pair_happy_path(session_factory):
    """Two un-linked rows, opposite types, equal amounts → 201 + both linked rows."""
    seed = await _seed_base(session_factory)
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("25.00"),
    )
    income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("25.00"),
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/transactions/pair",
            json={"expense_id": expense_id, "income_id": income_id},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert isinstance(body, list) and len(body) == 2
    # Sorted by id
    assert body[0]["id"] < body[1]["id"]
    ids = {row["id"] for row in body}
    assert ids == {expense_id, income_id}
    # Both legs link to each other
    by_id = {row["id"]: row for row in body}
    assert by_id[expense_id]["linked_transaction_id"] == income_id
    assert by_id[income_id]["linked_transaction_id"] == expense_id
    # Recategorized to system Transfer (default recategorize=True)
    assert by_id[expense_id]["category_id"] == seed["cat_transfer_id"]
    assert by_id[income_id]["category_id"] == seed["cat_transfer_id"]


# ── 2. POST /pair extra=forbid ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_pair_rejects_extra_fields(session_factory):
    """Body with unknown field → 422 (Pydantic extra=forbid)."""
    seed = await _seed_base(session_factory)
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("10.00"),
    )
    income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("10.00"),
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/transactions/pair",
            json={
                "expense_id": expense_id, "income_id": income_id,
                "extra": "nope",
            },
        )
    assert res.status_code == 422


# ── 3. POST /pair amount mismatch → 400 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_post_pair_validation_error_to_400(session_factory):
    """ValidationError from _link_pair invariant (unequal amounts) → HTTP 400."""
    seed = await _seed_base(session_factory)
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("25.00"),
    )
    income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("99.99"),
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/transactions/pair",
            json={"expense_id": expense_id, "income_id": income_id},
        )
    assert res.status_code == 400
    assert "equal absolute amounts" in res.json()["detail"].lower()


# ── 4. POST /{id}/convert-to-transfer pair-with path ────────────────────────


@pytest.mark.asyncio
async def test_post_convert_pair_with_existing(session_factory):
    """pair_with_transaction_id set → calls pair primitive, returns linked pair."""
    seed = await _seed_base(session_factory)
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("40.00"),
    )
    partner_income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("40.00"),
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{expense_id}/convert-to-transfer",
            json={
                "destination_account_id": seed["a2_id"],
                "pair_with_transaction_id": partner_income_id,
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert {row["id"] for row in body} == {expense_id, partner_income_id}
    by_id = {row["id"]: row for row in body}
    assert by_id[expense_id]["linked_transaction_id"] == partner_income_id
    assert by_id[partner_income_id]["linked_transaction_id"] == expense_id


# ── 5. POST /{id}/convert-to-transfer mismatched destination ───────────────


@pytest.mark.asyncio
async def test_post_convert_pair_with_mismatched_account(session_factory):
    """pair_with_transaction_id account != destination_account_id → 400."""
    seed = await _seed_base(session_factory)
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("40.00"),
    )
    # Partner is on a2, but request claims destination = a3.
    partner_income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("40.00"),
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{expense_id}/convert-to-transfer",
            json={
                "destination_account_id": seed["a3_id"],
                "pair_with_transaction_id": partner_income_id,
            },
        )
    assert res.status_code == 400
    assert "destination_account_id" in res.json()["detail"]


# ── 6. POST /{id}/convert-to-transfer create-missing-leg path ──────────────


@pytest.mark.asyncio
async def test_post_convert_create_missing_leg(session_factory):
    """No pair_with → calls convert_and_create_leg → 201 with both rows."""
    seed = await _seed_base(session_factory)
    source_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("60.00"), description="moved",
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{source_id}/convert-to-transfer",
            json={"destination_account_id": seed["a2_id"]},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert len(body) == 2
    by_id = {row["id"]: row for row in body}
    # Original source still in response, with link populated.
    assert source_id in by_id
    src = by_id[source_id]
    assert src["linked_transaction_id"] is not None
    partner_id = src["linked_transaction_id"]
    assert partner_id in by_id
    partner = by_id[partner_id]
    assert partner["account_id"] == seed["a2_id"]
    assert partner["type"] == "income"  # mirror of EXPENSE source
    assert Decimal(partner["amount"]) == Decimal("60.00")


# ── 7. POST /{id}/unpair returns both legs with new categories ─────────────


@pytest.mark.asyncio
async def test_post_unpair(session_factory):
    """Returns both legs with linked_transaction_id=None and fallback categories."""
    seed = await _seed_base(session_factory)
    # Seed an already-linked pair manually.
    expense_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_transfer_id"], type=TransactionType.EXPENSE,
        amount=Decimal("15.00"),
    )
    income_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_transfer_id"], type=TransactionType.INCOME,
        amount=Decimal("15.00"),
    )
    async with session_factory() as db:
        from sqlalchemy import select as _select
        e = (await db.execute(_select(Transaction).where(Transaction.id == expense_id))).scalar_one()
        i = (await db.execute(_select(Transaction).where(Transaction.id == income_id))).scalar_one()
        e.linked_transaction_id = i.id
        i.linked_transaction_id = e.id
        await db.commit()

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{expense_id}/unpair",
            json={
                "expense_fallback_category_id": seed["cat_groceries_id"],
                "income_fallback_category_id": seed["cat_salary_id"],
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 2
    by_id = {row["id"]: row for row in body}
    assert by_id[expense_id]["linked_transaction_id"] is None
    assert by_id[income_id]["linked_transaction_id"] is None
    assert by_id[expense_id]["category_id"] == seed["cat_groceries_id"]
    assert by_id[income_id]["category_id"] == seed["cat_salary_id"]


# ── 8. GET /{id}/transfer-candidates ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_transfer_candidates(session_factory):
    """Returns candidates filtered by destination_account_id with confidence labels."""
    seed = await _seed_base(session_factory)
    base_date = date(2026, 5, 1)
    # The source row: an expense on a1.
    source_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a1_id"],
        category_id=seed["cat_groceries_id"], type=TransactionType.EXPENSE,
        amount=Decimal("50.00"), description="paid", on_date=base_date,
    )
    # Same-day income on a2 → candidate, confidence=same_day.
    same_day_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("50.00"), description="received same day",
        on_date=base_date,
    )
    # +2 day income on a2 → candidate, confidence=near_date.
    near_id = await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a2_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("50.00"), description="received 2 days later",
        on_date=base_date + timedelta(days=2),
    )
    # Income on a3 (USD) → filtered out by destination_account_id.
    await _add_tx(
        session_factory,
        org_id=seed["org_id"], account_id=seed["a3_id"],
        category_id=seed["cat_salary_id"], type=TransactionType.INCOME,
        amount=Decimal("50.00"), description="usd account",
        on_date=base_date,
    )

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.get(
            f"/api/v1/transactions/{source_id}/transfer-candidates",
            params={"destination_account_id": seed["a2_id"]},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "candidates" in body
    cands = body["candidates"]
    cand_ids = [c["id"] for c in cands]
    assert same_day_id in cand_ids
    assert near_id in cand_ids
    # Sorted by date proximity (same_day first).
    assert cand_ids[0] == same_day_id
    by_id = {c["id"]: c for c in cands}
    assert by_id[same_day_id]["confidence"] == "same_day"
    assert by_id[same_day_id]["date_diff_days"] == 0
    assert by_id[near_id]["confidence"] == "near_date"
    assert by_id[near_id]["date_diff_days"] == 2
    # All candidates are on the requested destination account.
    assert all(c["account_id"] == seed["a2_id"] for c in cands)
