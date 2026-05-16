"""Edit Account Type — PUT cascade + POST create-path validation.

Covers spec ``specs/2026-05-09-edit-account-type.md`` § 3.1, § 3.1.1,
§ 4.2, § 6, and the test plan in § 8.1. Backend stack mirrors
``test_account_opening_balance.py``: FastAPI + SQLAlchemy 2.0 async
against in-memory aiosqlite (SQLite ignores ``with_for_update()``,
which is fine — the row-lock semantics are a MySQL/Postgres concern
and the spec already accepts that the locking test runs against the
real DB; here we only assert the cascade-logic invariants and the
audit-payload shape).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Organization
from app.models.audit_event import AuditEvent
from app.models.base import Base
from app.models.category import Category, CategoryType
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.security import hash_password


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest_asyncio.fixture
async def seeded(session_factory) -> dict:
    """Seed org + admin user + system account types (checking, credit_card,
    savings) + two accounts: a Checking account and a Credit Card account
    with ``close_day=15``. Tests then PUT against these to exercise the
    cascade matrix in both directions.
    """
    async with session_factory() as db:
        org = Organization(name="ECT Test Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()

        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@ect.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
        )
        db.add(admin)

        at_checking = AccountType(
            org_id=org.id, name="Checking", slug="checking", is_system=True
        )
        at_cc = AccountType(
            org_id=org.id, name="Credit Card", slug="credit_card", is_system=True
        )
        at_savings = AccountType(
            org_id=org.id, name="Savings", slug="savings", is_system=True
        )
        db.add_all([at_checking, at_cc, at_savings])
        await db.flush()

        # Transaction.category_id is NOT NULL — seed one default category
        # so the pending-tx test can attach rows. Slug is a system seed
        # value; nothing in this test branches on it.
        cat_expense = Category(
            org_id=org.id,
            name="General",
            slug="general",
            type=CategoryType.EXPENSE,
            is_system=False,
        )
        cat_income = Category(
            org_id=org.id,
            name="Income",
            slug="income",
            type=CategoryType.INCOME,
            is_system=False,
        )
        db.add_all([cat_expense, cat_income])
        await db.flush()

        checking_acct = Account(
            org_id=org.id,
            account_type_id=at_checking.id,
            name="Main Checking",
            balance=Decimal("100.00"),
            currency="EUR",
            is_active=True,
            close_day=None,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        cc_acct = Account(
            org_id=org.id,
            account_type_id=at_cc.id,
            name="Visa",
            balance=Decimal("-50.00"),
            currency="EUR",
            is_active=True,
            close_day=15,
            opening_balance=Decimal("0.00"),
            opening_balance_date=date(2026, 1, 1),
        )
        db.add_all([checking_acct, cc_acct])

        # Second org for cross-tenant checks.
        other_org = Organization(name="Other Org", billing_cycle_day=1)
        db.add(other_org)
        await db.flush()
        at_other = AccountType(
            org_id=other_org.id, name="Checking", slug="checking", is_system=True
        )
        db.add(at_other)

        await db.commit()

        return {
            "org_id": org.id,
            "admin_id": admin.id,
            "checking_type_id": at_checking.id,
            "cc_type_id": at_cc.id,
            "savings_type_id": at_savings.id,
            "checking_acct_id": checking_acct.id,
            "cc_acct_id": cc_acct.id,
            "other_org_id": other_org.id,
            "other_org_type_id": at_other.id,
            "expense_cat_id": cat_expense.id,
            "income_cat_id": cat_income.id,
        }


def _make_app(session_factory, *, as_other_org: bool = False) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            if as_other_org:
                # No user exists for the second org; the cross-tenant
                # tests use the admin user's org_id mismatched against
                # the other-org's account.
                pass
            return (
                await db.execute(select(User).where(User.role == Role.ADMIN))
            ).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return app


async def _audit_rows_for_type_changed(session_factory, account_id: int):
    async with session_factory() as db:
        return (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "account.type_changed",
                )
            )
        ).scalars().all()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── PUT cascade (test 1..13 from § 8.1) ──────────────────────────────────


def test_change_type_checking_to_credit_card_with_close_day(session_factory, seeded):
    """§ 8.1 #1 — happy path entering credit_card. 200, close_day set,
    audit row carries ``closes_day_set``."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"], "close_day": 20},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_type_id"] == seeded["cc_type_id"]
    assert body["account_type_slug"] == "credit_card"
    assert body["close_day"] == 20

    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["checking_acct_id"]))
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["account_id"] == seeded["checking_acct_id"]
    assert detail["old_type_slug"] == "checking"
    assert detail["new_type_slug"] == "credit_card"
    assert detail["closes_day_set"] == 20
    assert detail["closes_day_cleared"] is None


def test_change_type_checking_to_credit_card_missing_close_day(
    session_factory, seeded
):
    """§ 8.1 #2 — entering credit_card without close_day -> 400, no mutation."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"]},
        )
    assert res.status_code == 400
    assert "close_day is required" in res.json()["detail"]

    # Account unchanged.
    async def _fetch():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(Account).where(Account.id == seeded["checking_acct_id"])
                )
            ).scalar_one()

    acct = _run(_fetch())
    assert acct.account_type_id == seeded["checking_type_id"]
    assert acct.close_day is None


def test_change_type_credit_card_to_checking_clears_close_day(
    session_factory, seeded
):
    """§ 8.1 #3 — leaving credit_card clears close_day; audit detail
    carries ``closes_day_cleared`` with the old value."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={"account_type_id": seeded["checking_type_id"]},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_type_slug"] == "checking"
    assert body["close_day"] is None

    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["cc_acct_id"]))
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["closes_day_cleared"] == 15
    assert detail["closes_day_set"] is None


def test_change_type_credit_card_to_checking_payload_carries_close_day(
    session_factory, seeded
):
    """§ 8.1 #4 — leaving credit_card with payload close_day=N -> 400."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={
                "account_type_id": seeded["checking_type_id"],
                "close_day": 10,
            },
        )
    assert res.status_code == 400
    assert "close_day is only allowed on credit_card accounts" in res.json()["detail"]


def test_change_type_to_invalid_type_id(session_factory, seeded):
    """§ 8.1 #5 — non-existent type id -> 422."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": 999999},
        )
    assert res.status_code == 422
    assert "Invalid account type" in res.json()["detail"]


def test_change_type_to_other_org_type_id(session_factory, seeded):
    """§ 8.1 #6 — cross-org type id -> 422; response body must not echo
    the foreign type's name."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["other_org_type_id"]},
        )
    assert res.status_code == 422
    # The error body should NOT echo the other-org type's name. Our
    # message is the generic "Invalid account type"; assert that nothing
    # like a leaked-name string crept in.
    body = res.json()
    assert "Other Org" not in str(body)
    assert "Invalid account type" in body["detail"]


def test_change_type_no_op_same_type_id(session_factory, seeded):
    """§ 8.1 #7 — PUT with the same type id is a no-op on type. No
    ``account.type_changed`` audit row."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        # The checking account has close_day=None; sending the same type
        # without close_day must not trip the cascade.
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["checking_type_id"]},
        )
    assert res.status_code == 200, res.text

    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["checking_acct_id"]))
    assert rows == [], "no-op type call must not emit account.type_changed"


def test_change_type_close_day_out_of_range(session_factory, seeded):
    """§ 8.1 #8 — close_day < 1 or > 28 -> Pydantic 422, no DB write."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"], "close_day": 99},
        )
    assert res.status_code == 422


def test_change_type_with_pending_transactions_does_not_settle_them(
    session_factory, seeded
):
    """§ 8.1 #9 — pending tx on a CC account stays pending across a type
    change to Checking. Balance is unaffected."""

    async def _seed_pending():
        async with session_factory() as db:
            for _ in range(3):
                db.add(
                    Transaction(
                        org_id=seeded["org_id"],
                        account_id=seeded["cc_acct_id"],
                        category_id=seeded["expense_cat_id"],
                        amount=Decimal("10.00"),
                        type=TransactionType.EXPENSE,
                        status=TransactionStatus.PENDING,
                        description="cc pending",
                        date=date(2026, 5, 1),
                    )
                )
            await db.commit()

    _run(_seed_pending())

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={"account_type_id": seeded["checking_type_id"]},
        )
    assert res.status_code == 200, res.text

    async def _check_pending():
        async with session_factory() as db:
            tx_rows = (
                await db.execute(
                    select(Transaction).where(
                        Transaction.account_id == seeded["cc_acct_id"]
                    )
                )
            ).scalars().all()
            acct = (
                await db.execute(
                    select(Account).where(Account.id == seeded["cc_acct_id"])
                )
            ).scalar_one()
            return tx_rows, acct

    rows, acct = _run(_check_pending())
    assert len(rows) == 3
    assert all(r.status == TransactionStatus.PENDING for r in rows)
    assert acct.balance == Decimal("-50.00")  # untouched by the type change


def test_change_type_audit_event_persisted(session_factory, seeded):
    """§ 8.1 #10 — assert one row with the spec's full audit shape."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"], "close_day": 5},
        )

    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["checking_acct_id"]))
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == seeded["admin_id"]
    assert row.target_org_id == seeded["org_id"]
    assert row.outcome.value == "success"
    detail = row.detail
    for key in (
        "account_id",
        "old_type_id",
        "new_type_id",
        "old_type_slug",
        "new_type_slug",
        "closes_day_set",
        "closes_day_cleared",
    ):
        assert key in detail


def test_change_type_account_not_found(session_factory, seeded):
    """§ 8.1 #11 (adapted) — account does not exist -> 404."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/accounts/9999",
            json={"account_type_id": seeded["cc_type_id"], "close_day": 10},
        )
    assert res.status_code == 404


def test_change_type_does_not_touch_balance_or_transaction_count(
    session_factory, seeded
):
    """§ 8.1 #12 — balance + transaction count unchanged pre/post."""

    async def _seed_and_snapshot():
        async with session_factory() as db:
            for i in range(2):
                db.add(
                    Transaction(
                        org_id=seeded["org_id"],
                        account_id=seeded["checking_acct_id"],
                        category_id=seeded["income_cat_id"],
                        amount=Decimal("25.00"),
                        type=TransactionType.INCOME,
                        status=TransactionStatus.SETTLED,
                        description=f"snap-{i}",
                        date=date(2026, 5, 1),
                        settled_date=date(2026, 5, 1),
                    )
                )
            await db.commit()
            acct = (
                await db.execute(
                    select(Account).where(Account.id == seeded["checking_acct_id"])
                )
            ).scalar_one()
            count = len(
                (
                    await db.execute(
                        select(Transaction).where(
                            Transaction.account_id == seeded["checking_acct_id"]
                        )
                    )
                ).scalars().all()
            )
            return acct.balance, count

    pre_balance, pre_count = _run(_seed_and_snapshot())

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"], "close_day": 5},
        )
    assert res.status_code == 200, res.text

    async def _snapshot_after():
        async with session_factory() as db:
            acct = (
                await db.execute(
                    select(Account).where(Account.id == seeded["checking_acct_id"])
                )
            ).scalar_one()
            count = len(
                (
                    await db.execute(
                        select(Transaction).where(
                            Transaction.account_id == seeded["checking_acct_id"]
                        )
                    )
                ).scalars().all()
            )
            return acct.balance, count

    post_balance, post_count = _run(_snapshot_after())
    assert post_balance == pre_balance
    assert post_count == pre_count


def test_close_day_only_edit_on_non_cc_account_rejected(session_factory, seeded):
    """§ 8.1 #13 — body {close_day: 15} on Checking account -> 400.
    Closes the silent-swallow hole in the previous PUT path."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={"close_day": 15},
        )
    assert res.status_code == 400
    assert "close_day is only allowed on credit_card accounts" in res.json()["detail"]


# ── PR #246 second review: CC -> CC no-op close_day contract ────────────


def test_credit_card_to_credit_card_omitted_close_day_preserves_existing_value(
    session_factory, seeded
):
    """Spec § 3.1 row 3 (CC -> CC) — payload MAY omit close_day; the
    existing value must remain untouched. PR #246 second-review fix:
    previously the validator required close_day on every CC-target
    PUT, including pure no-op type updates.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={"account_type_id": seeded["cc_type_id"]},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_type_id"] == seeded["cc_type_id"]
    assert body["close_day"] == 15  # original value preserved

    # No type-changed audit (this is a no-op on type).
    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["cc_acct_id"]))
    assert rows == []


def test_credit_card_to_credit_card_explicit_null_close_day_is_400(
    session_factory, seeded
):
    """Spec § 3.1 row 3 (CC -> CC) with explicit ``close_day: null`` is
    invalid: a credit_card row must keep a non-null close_day.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={
                "account_type_id": seeded["cc_type_id"],
                "close_day": None,
            },
        )
    assert res.status_code == 400
    assert "close_day is required" in res.json()["detail"]

    # Account unchanged.
    async def _refetch():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(Account).where(Account.id == seeded["cc_acct_id"])
                )
            ).scalar_one()

    acct = _run(_refetch())
    assert acct.close_day == 15
    assert acct.account_type_id == seeded["cc_type_id"]


def test_credit_card_to_credit_card_explicit_close_day_updates_value(
    session_factory, seeded
):
    """Spec § 3.1 row 3 (CC -> CC) with an explicit non-null close_day
    updates the column in-place. No type-changed audit (same type).
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['cc_acct_id']}",
            json={
                "account_type_id": seeded["cc_type_id"],
                "close_day": 22,
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["close_day"] == 22

    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["cc_acct_id"]))
    assert rows == []


# ── PR #246 review: P1 atomicity regression ─────────────────────────────


def test_mixed_type_change_plus_deactivate_with_balance_rolls_back_atomically(
    session_factory, seeded
):
    """Regression for PR #246 review P1: a single PUT that flips the
    account type AND tries to deactivate an account with nonzero balance
    must roll back the type change atomically and emit no
    ``account.type_changed`` audit row.

    Before the fix the service-owned ``change_account_type()`` committed
    its transaction before the route reached the 409 nonzero-balance
    guard, leaving the row half-changed and producing a stray audit
    row.

    The seeded Checking account has balance=100.00, so ``is_active=False``
    must 409. Assert: (a) response is 409, (b) type stays Checking, (c)
    no audit row.
    """
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/accounts/{seeded['checking_acct_id']}",
            json={
                "account_type_id": seeded["cc_type_id"],
                "close_day": 15,
                "is_active": False,
            },
        )
    assert res.status_code == 409, res.text
    assert "Cannot deactivate" in res.json()["detail"]

    # (b) account row was NOT mutated. The atomic refactor in the
    # router rolls back the locked type-change write when the
    # is_active guard raises.
    async def _refetch():
        async with session_factory() as db:
            return (
                await db.execute(
                    select(Account).where(Account.id == seeded["checking_acct_id"])
                )
            ).scalar_one()

    acct = _run(_refetch())
    assert acct.account_type_id == seeded["checking_type_id"]
    assert acct.close_day is None
    assert acct.is_active is True

    # (c) no audit row for the aborted type change.
    rows = _run(_audit_rows_for_type_changed(session_factory, seeded["checking_acct_id"]))
    assert rows == [], (
        "atomic rollback must suppress the account.type_changed audit row; "
        f"found {len(rows)} row(s) instead"
    )


# ── POST create-path validation (§ 3.1.1, tests 14..18) ──────────────────


def test_create_account_credit_card_missing_close_day(session_factory, seeded):
    """§ 8.1 #14 — POST credit_card with no close_day -> 400."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "New CC",
                "account_type_id": seeded["cc_type_id"],
                "currency": "EUR",
            },
        )
    assert res.status_code == 400
    assert "close_day is required" in res.json()["detail"]

    # No row inserted.
    async def _count():
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(Account).where(Account.name == "New CC")
                )
            ).scalars().all()
            return len(rows)

    assert _run(_count()) == 0


def test_create_account_credit_card_close_day_null(session_factory, seeded):
    """§ 8.1 #15 — POST credit_card with close_day=null -> 400. Distinct
    payload shape from #14 (field present but null)."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "New CC Null",
                "account_type_id": seeded["cc_type_id"],
                "currency": "EUR",
                "close_day": None,
            },
        )
    assert res.status_code == 400
    assert "close_day is required" in res.json()["detail"]


def test_create_account_non_credit_card_with_close_day(session_factory, seeded):
    """§ 8.1 #16 — POST Checking with close_day=15 -> 400."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "Bad Checking",
                "account_type_id": seeded["checking_type_id"],
                "currency": "EUR",
                "close_day": 15,
            },
        )
    assert res.status_code == 400
    assert "close_day is only allowed on credit_card accounts" in res.json()["detail"]


def test_create_account_credit_card_with_close_day_succeeds(session_factory, seeded):
    """§ 8.1 #17 — happy path 201, row inserted with close_day=15."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "New CC OK",
                "account_type_id": seeded["cc_type_id"],
                "currency": "EUR",
                "close_day": 15,
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["close_day"] == 15
    assert body["account_type_slug"] == "credit_card"


def test_create_account_non_credit_card_without_close_day_succeeds(
    session_factory, seeded
):
    """§ 8.1 #18 — happy path 201, row inserted with close_day=NULL."""
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/accounts",
            json={
                "name": "New Checking",
                "account_type_id": seeded["checking_type_id"],
                "currency": "EUR",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["close_day"] is None
    assert body["account_type_slug"] == "checking"


# ── § 8.1 #19 row-lock concurrency ───────────────────────────────────────


# SQLite + aiosqlite does not honour `with_for_update()` (no row-locking
# semantics on SQLite in StaticPool single-conn test mode), so the
# race-window assertion the spec calls for is enforced at the MySQL/Postgres
# layer in production. We document this here in lieu of a passing test
# against the in-memory engine.
@pytest.mark.skip(
    reason="SQLite test backend does not honor FOR UPDATE; concurrency is "
    "enforced at MySQL in prod. Manual smoke covers this path."
)
def test_concurrent_type_changes_serialize_on_row_lock(session_factory, seeded):
    pass
