"""``POST /api/v1/transactions/{id}/unpair`` reciprocity guard (TBD-281).

``unpair_transactions`` gated on non-nullness alone, then rewrote BOTH
legs' ``category_id`` and NULLed both link columns. A one-way link -- the
normal output of ``reconciliation_service._apply_match`` -- is not a
transfer pair, so unpairing it recategorized an unrelated canonical row.

A one-way link is refused with **400**, not 409:

* there is no representable no-op -- the endpoint returns exactly two
  legs, and returning the unrelated canonical row would invent a pair;
* ``ConflictError`` means "refresh and retry" and clients may auto-retry.
  Retrying can never help. 409 stays for the genuine race.

Fixture notes (design §6.0): the fallback categories DIFFER from both
rows' current category, so "raise after mutating" is visible; assertions
compare category **ids**; transaction ids are explicit and start at 9001
so nothing can pass by matching id ``1``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
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
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.transactions import router as transactions_router
from app.security import hash_password
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


def make_app(session_factory) -> FastAPI:
    """Minimal app with the same domain-exception handlers the real app
    registers, so the 400 / 404 / 409 split is observable."""
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

    app.include_router(transactions_router)
    return app


# ── Fixture ─────────────────────────────────────────────────────────────────

# Different opening balances per account: a revert attributed to the wrong
# account cannot hide behind an equal starting value.
ACCT_A_OPENING = Decimal("1000.00")
ACCT_B_OPENING = Decimal("250.00")


async def _seed(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Test Org", billing_cycle_day=1)
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
        a1 = Account(
            org_id=org.id, name="Acct A", account_type_id=at.id,
            balance=ACCT_A_OPENING, currency="EUR",
        )
        a2 = Account(
            org_id=org.id, name="Acct B", account_type_id=at.id,
            balance=ACCT_B_OPENING, currency="EUR",
        )
        db.add_all([a1, a2])
        await db.flush()
        # The two legs start on the system Transfer category; both fallbacks
        # DIFFER from it, so any premature write is visible.
        cat_transfer = Category(
            org_id=org.id, name="Transfer", slug="transfer",
            type=CategoryType.BOTH, is_system=True,
        )
        cat_groceries = Category(
            org_id=org.id, name="Groceries", slug="groceries",
            type=CategoryType.EXPENSE, is_system=False,
        )
        cat_salary = Category(
            org_id=org.id, name="Salary", slug="salary",
            type=CategoryType.INCOME, is_system=False,
        )
        db.add_all([cat_transfer, cat_groceries, cat_salary])
        await db.commit()
        return {
            "org_id": org.id,
            "a1_id": a1.id,
            "a2_id": a2.id,
            "cat_transfer_id": cat_transfer.id,
            "cat_groceries_id": cat_groceries.id,
            "cat_salary_id": cat_salary.id,
        }


async def _add(
    factory, seed: dict, *, tx_id: int, account_id: int,
    tx_type: TransactionType, amount: str,
) -> int:
    async with factory() as db:
        db.add(
            Transaction(
                id=tx_id,
                org_id=seed["org_id"],
                account_id=account_id,
                category_id=seed["cat_transfer_id"],
                description=f"row-{tx_id}",
                amount=Decimal(amount),
                type=tx_type,
                status=TransactionStatus.SETTLED,
                date=date(2026, 5, 1),
                settled_date=date(2026, 5, 1),
            )
        )
        await db.commit()
    return tx_id


async def _link(factory, *, src_id: int, dst_id: int | None) -> None:
    async with factory() as db:
        row = (
            await db.execute(select(Transaction).where(Transaction.id == src_id))
        ).scalar_one()
        row.linked_transaction_id = dst_id
        await db.commit()


async def _row(factory, tx_id: int) -> Transaction:
    async with factory() as db:
        return (
            await db.execute(select(Transaction).where(Transaction.id == tx_id))
        ).scalar_one()


def _unpair(client, tx_id: int, seed: dict):
    return client.post(
        f"/api/v1/transactions/{tx_id}/unpair",
        json={
            "expense_fallback_category_id": seed["cat_groceries_id"],
            "income_fallback_category_id": seed["cat_salary_id"],
        },
    )


NOT_A_PAIR = "Transaction is not part of a transfer pair"


# ── F8 / F10 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unpair_refuses_a_one_way_link_and_mutates_nothing(session_factory):
    """F8. Kills: the null-only check restored at ``unpair_transactions``.

    F10. Kills: ``ConflictError`` (409) instead of ``ValidationError``
    (400) -- asserted as **exactly** 400, never ``>= 400``.

    ``M -> T`` is the normal output of ``_apply_match``: M points at the
    canonical row T, T points nowhere. Unpairing it used to rewrite
    BOTH rows' ``category_id``. Re-reading both rows afterwards is what
    kills a "raise after mutating" implementation.
    """
    seed = await _seed(session_factory)
    m = await _add(session_factory, seed, tx_id=9001, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="8.00")
    t = await _add(session_factory, seed, tx_id=9002, account_id=seed["a2_id"],
                   tx_type=TransactionType.INCOME, amount="8.00")
    await _link(session_factory, src_id=m, dst_id=t)   # ONE-WAY

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = _unpair(client, m, seed)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == NOT_A_PAIR

    m_row = await _row(session_factory, m)
    t_row = await _row(session_factory, t)
    assert m_row.category_id == seed["cat_transfer_id"]
    assert t_row.category_id == seed["cat_transfer_id"]
    assert m_row.linked_transaction_id == t
    assert t_row.linked_transaction_id is None


# ── F9 ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unpair_reports_the_link_not_the_type_composition(session_factory):
    """F9. Kills: the reciprocity check placed AFTER ``rows_by_type``.

    Two SAME-TYPE rows joined by a one-way link already fail today -- but
    with 409 "Pair has invalid type composition", which blames the data
    for what is really a bad request. The check must precede the
    type-composition test.
    """
    seed = await _seed(session_factory)
    m = await _add(session_factory, seed, tx_id=9011, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="16.00")
    t = await _add(session_factory, seed, tx_id=9012, account_id=seed["a2_id"],
                   tx_type=TransactionType.EXPENSE, amount="16.00")
    await _link(session_factory, src_id=m, dst_id=t)   # ONE-WAY, same type

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = _unpair(client, m, seed)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == NOT_A_PAIR
    assert "type composition" not in res.json()["detail"]


# ── F9b ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unpair_of_a_self_linked_row_is_400_not_409(session_factory):
    """F9b. Kills: the reciprocity check placed AFTER the
    ``len(rows) != 2`` race test.

    ``sorted([id, id])`` collapses to ONE row, so a self-linked row trips
    the race branch and reports 409 "Pair partner not found" -- a lie:
    nothing raced, the row is simply not a pair.
    """
    seed = await _seed(session_factory)
    s = await _add(session_factory, seed, tx_id=9021, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="32.00")
    await _link(session_factory, src_id=s, dst_id=s)   # self-link

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = _unpair(client, s, seed)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == NOT_A_PAIR
    assert "Pair partner not found" not in res.json()["detail"]

    s_row = await _row(session_factory, s)
    assert s_row.category_id == seed["cat_transfer_id"]
    assert s_row.linked_transaction_id == s


# ── F21: the PREVIEW check, killed on its own ───────────────────────────────


@pytest.mark.asyncio
async def test_unpair_preview_check_runs_before_category_validation(session_factory):
    """F21. Kills: the PRE-LOCK PREVIEW reciprocity check deleted, on its
    own -- with the authoritative post-``FOR UPDATE`` check left in place.

    ⚠ Why this test exists. Measured: F8 / F9 / F9b / F10 above stay fully
    green when EITHER of the two checks is deleted; only deleting BOTH
    turns them red. What they actually pin is "some reciprocity check
    exists somewhere before the mutation", not the check their docstrings
    name. This one discriminates by POSITION.

    The lever is the fallback category ids: they are bogus, so
    ``validate_category`` -- which sits BETWEEN the two checks -- refuses
    the request first if the preview check is not there to refuse it.

    * preview check present  -> 400 "Transaction is not part of a
      transfer pair"
    * preview check deleted  -> 400 "Invalid category" *(measured)*,
      because category validation is now the first thing a one-way link
      hits; the authoritative post-lock check never gets a say.

    ⚠ Both outcomes are **400** -- ``validate_category`` raises
    ``ValidationError``, not ``NotFoundError``. The discriminator is
    therefore the DETAIL STRING, and ``assert res.json()["detail"] ==
    NOT_A_PAIR`` is the line that kills the mutant. A ``status_code``
    assertion alone would be vacuous here; do not "simplify" it away.

    (The authoritative post-lock check has no such single-threaded fence
    and cannot be given one -- see design §6.4.)
    """
    seed = await _seed(session_factory)
    m = await _add(session_factory, seed, tx_id=9051, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="256.00")
    t = await _add(session_factory, seed, tx_id=9052, account_id=seed["a2_id"],
                   tx_type=TransactionType.INCOME, amount="256.00")
    await _link(session_factory, src_id=m, dst_id=t)   # ONE-WAY

    bogus_expense_cat = seed["cat_groceries_id"] + 90_001
    bogus_income_cat = seed["cat_salary_id"] + 90_002

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/transactions/{m}/unpair",
            json={
                "expense_fallback_category_id": bogus_expense_cat,
                "income_fallback_category_id": bogus_income_cat,
            },
        )

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == NOT_A_PAIR


# ── The guard must not be too strict ────────────────────────────────────────


@pytest.mark.asyncio
async def test_unpair_of_a_real_reciprocal_pair_still_succeeds(session_factory):
    """The positive face: a genuine bidirectional pair still unpairs, with
    each leg landing on its own type-matched fallback category."""
    seed = await _seed(session_factory)
    e = await _add(session_factory, seed, tx_id=9031, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="64.00")
    i = await _add(session_factory, seed, tx_id=9032, account_id=seed["a2_id"],
                   tx_type=TransactionType.INCOME, amount="64.00")
    await _link(session_factory, src_id=e, dst_id=i)
    await _link(session_factory, src_id=i, dst_id=e)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = _unpair(client, e, seed)

    assert res.status_code == 200, res.text
    by_id = {row["id"]: row for row in res.json()}
    assert set(by_id) == {e, i}
    assert by_id[e]["linked_transaction_id"] is None
    assert by_id[i]["linked_transaction_id"] is None
    assert by_id[e]["category_id"] == seed["cat_groceries_id"]
    assert by_id[i]["category_id"] == seed["cat_salary_id"]


@pytest.mark.asyncio
async def test_unpair_called_with_the_canonical_leg_of_a_one_way_link(session_factory):
    """The other end of the same one-way link: T's own link column is
    NULL, so the pre-existing null check refuses it. Same message, so the
    two ends of a non-pair are indistinguishable to the client -- at this
    layer we know the link is non-mutual, not why."""
    seed = await _seed(session_factory)
    m = await _add(session_factory, seed, tx_id=9041, account_id=seed["a1_id"],
                   tx_type=TransactionType.EXPENSE, amount="128.00")
    t = await _add(session_factory, seed, tx_id=9042, account_id=seed["a2_id"],
                   tx_type=TransactionType.INCOME, amount="128.00")
    await _link(session_factory, src_id=m, dst_id=t)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = _unpair(client, t, seed)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == NOT_A_PAIR
