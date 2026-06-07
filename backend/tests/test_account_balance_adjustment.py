"""Track E — manual balance adjustment for org admins.

Covers the entire surface of the new endpoint and the service-layer
helpers it sits on top of:

- Router-level permission gating, payload validation, error precedence.
- Service-level delta math, transaction generation, and audit row.
- Lazy ``balance-adjustment`` system category seeding under concurrency.
- Knock-on guards: standard CRUD refuses to mutate adjustment rows;
  bulk delete skips them silently; transfer-pair detection ignores them;
  promote_to_recurring rejects them; reconciliation INCLUDES them.

Backend stack: FastAPI + SQLAlchemy 2.0 async. Tests run against an
in-memory aiosqlite DB seeded from ``Base.metadata.create_all`` plus the
UNIQUE(org_id, slug, is_system) index that migration 035 adds (mirrored
here so the lazy-seed race test exercises real constraint behaviour).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Account, AccountType, Category, Organization, Transaction
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.base import Base
from app.models.category import CategoryType
from app.models.transaction import TransactionStatus, TransactionType
from app.models.user import Role, User
from app.routers.accounts import router as accounts_router
from app.security import hash_password
from app.services import transaction_service
from app.services.exceptions import ConflictError, ValidationError
from app.services.transaction_service import (
    BALANCE_ADJUSTMENT_CATEGORY_SLUG,
    adjust_account_balance,
    bulk_delete_transactions,
    delete_transaction,
    find_duplicate_of_linked_leg,
    find_match_candidates,
    get_or_create_balance_adjustment_category,
    promote_to_recurring,
    reconcile_account,
    update_transaction,
)
from app.schemas.transaction import (
    PromoteToRecurringRequest,
    TransactionUpdate,
)
from app.services.transaction_filters import (
    is_reportable_transaction,
    reportable_transaction_filter,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


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
        # Mirror migration 035's UNIQUE(org_id, slug, is_system) on
        # categories so the lazy-seed concurrency test exercises real
        # constraint behaviour.
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_categories_org_slug_system "
                "ON categories (org_id, slug, is_system)"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(session_factory) -> dict:
    """Seed an org with admin + member users, an account with a
    starting balance of 100, and an "Other" expense category for
    creating a regular tx in some tests.
    """
    async with session_factory() as db:
        org = Organization(
            name="Test Org",
            billing_cycle_day=1,
            allow_manual_balance_adjustment=True,
        )
        db.add(org)
        await db.flush()

        admin = User(
            org_id=org.id,
            username="admin",
            email="admin@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id,
            username="member",
            email="member@test.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_active=True,
            email_verified=True,
        )
        db.add_all([admin, member])

        at = AccountType(org_id=org.id, name="Checking", slug="checking", is_system=True)
        db.add(at)
        await db.flush()

        acct = Account(
            org_id=org.id,
            account_type_id=at.id,
            name="Primary",
            balance=Decimal("100.00"),
            currency="EUR",
            is_active=True,
        )
        db.add(acct)

        cat = Category(
            org_id=org.id,
            name="Other",
            slug="other",
            description="Misc",
            type=CategoryType.BOTH,
        )
        db.add(cat)

        await db.commit()

        return {
            "org_id": org.id,
            "admin_id": admin.id,
            "member_id": member.id,
            "account_id": acct.id,
            "category_id": cat.id,
        }


def _make_app(session_factory, current_user_resolver) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(accounts_router)
    return app


def _resolver_for(role: Role):
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.role == role))
            ).scalar_one()

    return resolve


# ── 1-2. Happy paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_positive_delta_creates_income_row_and_increases_balance(
    session_factory, seeded
):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00", "reason": "deposit"},
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["old_balance"]) == Decimal("100.00")
    assert Decimal(body["new_balance"]) == Decimal("150.00")
    assert Decimal(body["delta"]) == Decimal("50.00")

    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        assert acct.balance == Decimal("150.00")
        tx = (await db.execute(
            select(Transaction).where(Transaction.id == body["transaction_id"])
        )).scalar_one()
        assert tx.type == TransactionType.INCOME
        assert tx.amount == Decimal("50.00")
        assert tx.status == TransactionStatus.SETTLED
        assert tx.is_manual_adjustment is True
        assert tx.linked_transaction_id is None
        assert tx.recurring_id is None
        assert tx.is_imported is False
        assert "100.00 -> 150.00" in tx.description


@pytest.mark.asyncio
async def test_negative_delta_creates_expense_row_with_abs_amount(
    session_factory, seeded
):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "70.00"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["delta"]) == Decimal("-30.00")

    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        assert acct.balance == Decimal("70.00")
        tx = (await db.execute(
            select(Transaction).where(Transaction.id == body["transaction_id"])
        )).scalar_one()
        assert tx.type == TransactionType.EXPENSE
        assert tx.amount == Decimal("30.00")  # abs(delta)
        assert tx.is_manual_adjustment is True


# ── 3. Zero delta ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_delta_returns_409_no_tx_no_audit(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "100.00"},
        )
    assert res.status_code == 409
    assert "No change to apply" in res.json()["detail"]

    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        assert acct.balance == Decimal("100.00")
        tx_count = (await db.execute(
            select(Transaction).where(Transaction.org_id == seeded["org_id"])
        )).scalars().all()
        assert tx_count == []
        audits = (await db.execute(select(AuditEvent))).scalars().all()
        assert audits == []


# ── 4-5. Lazy category seeding ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lazy_category_first_call_creates_then_reuses(
    session_factory, seeded
):
    async with session_factory() as db:
        cid1 = await get_or_create_balance_adjustment_category(
            db, org_id=seeded["org_id"]
        )
        await db.commit()

    async with session_factory() as db:
        cid2 = await get_or_create_balance_adjustment_category(
            db, org_id=seeded["org_id"]
        )
        await db.commit()

    assert cid1 == cid2

    async with session_factory() as db:
        cats = (await db.execute(
            select(Category).where(
                Category.org_id == seeded["org_id"],
                Category.slug == BALANCE_ADJUSTMENT_CATEGORY_SLUG,
            )
        )).scalars().all()
        assert len(cats) == 1
        assert cats[0].is_system is True
        assert cats[0].type == CategoryType.BOTH


@pytest.mark.asyncio
async def test_lazy_category_loser_retries_on_integrity_error(
    session_factory, seeded
):
    """Simulates the race outcome: a category row already exists (the
    "winner" committed first). A subsequent caller whose pre-SELECT
    misses the row (e.g. because it ran before the winner committed)
    bypasses the early-return, reaches the INSERT, hits the UNIQUE
    constraint, and retries via SELECT.

    The two-session sequence below is the deterministic equivalent of
    that race; ``asyncio.gather`` on ``StaticPool`` aiosqlite serializes
    real concurrency and isn't a faithful repro. The architect's spec
    item is the **branch coverage** of the IntegrityError-retry path,
    which this exercises.
    """
    # Winner commits first.
    async with session_factory() as db:
        winner_cid = await get_or_create_balance_adjustment_category(
            db, org_id=seeded["org_id"]
        )
        await db.commit()

    # Loser path: clear the SELECT cache by opening a fresh session
    # and force the INSERT branch by patching the early-return SELECT
    # to return None on first call (simulating "didn't see winner yet").
    from app.services import transaction_service as svc

    real_scalar = AsyncSession.scalar
    call_count = {"n": 0}

    async def fake_scalar(self, statement, *args, **kwargs):
        # First scalar() in the helper is the early-return SELECT;
        # force it to miss so the helper races into INSERT and trips
        # the unique constraint.
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        return await real_scalar(self, statement, *args, **kwargs)

    async with session_factory() as db:
        # Patch the *unbound* method on AsyncSession so it intercepts
        # calls within the helper.
        AsyncSession.scalar = fake_scalar  # type: ignore[method-assign]
        try:
            loser_cid = await get_or_create_balance_adjustment_category(
                db, org_id=seeded["org_id"]
            )
            await db.commit()
        finally:
            AsyncSession.scalar = real_scalar  # type: ignore[method-assign]

    assert winner_cid == loser_cid

    async with session_factory() as db:
        cats = (await db.execute(
            select(Category).where(
                Category.org_id == seeded["org_id"],
                Category.slug == BALANCE_ADJUSTMENT_CATEGORY_SLUG,
            )
        )).scalars().all()
        assert len(cats) == 1


# ── 6-9. Permission and routing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_admin_blocked_with_admin_403(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required"


@pytest.mark.asyncio
async def test_admin_blocked_when_flag_off(session_factory, seeded):
    # Flip the flag off.
    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seeded["org_id"])
        )).scalar_one()
        org.allow_manual_balance_adjustment = False
        await db.commit()

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == (
        "Manual balance adjustment is disabled for this organization"
    )


@pytest.mark.asyncio
async def test_admin_403_wins_over_flag_403(session_factory, seeded):
    """Non-admin caller AND flag OFF → admin-403 wins (precedence)."""
    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seeded["org_id"])
        )).scalar_one()
        org.allow_manual_balance_adjustment = False
        await db.commit()

    app = _make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required"


# ── Guard-order precedence: admin-403 / flag-403 must fire BEFORE 422 ────


@pytest.mark.asyncio
async def test_invalid_body_as_non_admin_returns_403_not_422(session_factory, seeded):
    """Non-admin caller with malformed body → admin-403 wins over Pydantic 422.
    The architect-locked precedence is admin → flag → 422; parsing the body
    before the role check would invert that order."""
    app = _make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "99999999999999.99"},  # overflow → would be 422
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required"


@pytest.mark.asyncio
async def test_invalid_body_as_admin_with_flag_off_returns_403_not_422(
    session_factory, seeded
):
    """Admin caller with flag OFF + malformed body → flag-403 wins over 422."""
    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seeded["org_id"])
        )).scalar_one()
        org.allow_manual_balance_adjustment = False
        await db.commit()

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"reason": "x" * 201},  # missing required + over-length → 422
        )
    assert res.status_code == 403
    assert res.json()["detail"] == (
        "Manual balance adjustment is disabled for this organization"
    )


@pytest.mark.asyncio
async def test_malformed_json_as_non_admin_returns_403_not_422(session_factory, seeded):
    """Non-admin caller with non-JSON body → admin-403 still wins."""
    app = _make_app(session_factory, _resolver_for(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "Admin access required"


@pytest.mark.asyncio
async def test_invalid_body_with_full_permissions_returns_422(session_factory, seeded):
    """Admin + flag ON + malformed body → 422 (the gates pass, body validation
    is the lawful failure mode)."""
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "99999999999999.99"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_malformed_json_with_full_permissions_returns_422(session_factory, seeded):
    """Admin + flag ON + invalid JSON → 422 (not 500)."""
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_cross_org_account_404(session_factory, seeded):
    # Seed a second org with an account; the admin caller's org_id
    # doesn't match, so the path-level account_id should 404.
    async with session_factory() as db:
        other_org = Organization(name="Other", billing_cycle_day=1)
        db.add(other_org)
        await db.flush()
        at = AccountType(org_id=other_org.id, name="C", slug="c")
        db.add(at)
        await db.flush()
        other_acct = Account(
            org_id=other_org.id,
            account_type_id=at.id,
            name="X",
            balance=Decimal("0.00"),
            currency="EUR",
        )
        db.add(other_acct)
        await db.commit()
        other_acct_id = other_acct.id

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{other_acct_id}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 404
    assert res.json()["detail"] == "Account not found"


# ── 10-11. Pydantic validation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_balance_overflow_returns_422(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "99999999999999.99"},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_reason_over_200_chars_returns_422(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={
                "target_balance": "150.00",
                "reason": "x" * 201,
            },
        )
    assert res.status_code == 422


# ── 12-13. Description formatting ────────────────────────────────────────


@pytest.mark.asyncio
async def test_description_no_reason_has_no_parens(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 200, res.text
    tx_id = res.json()["transaction_id"]

    async with session_factory() as db:
        tx = (await db.execute(
            select(Transaction).where(Transaction.id == tx_id)
        )).scalar_one()
        assert tx.description == "Balance adjustment: 100.00 -> 150.00"


@pytest.mark.asyncio
async def test_description_with_reason_has_parens(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00", "reason": "deposit"},
        )
    assert res.status_code == 200, res.text
    tx_id = res.json()["transaction_id"]

    async with session_factory() as db:
        tx = (await db.execute(
            select(Transaction).where(Transaction.id == tx_id)
        )).scalar_one()
        assert tx.description == "Balance adjustment: 100.00 -> 150.00 (deposit)"


# ── 14-15. Audit row ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adjust_writes_audit_row(session_factory, seeded):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00", "reason": "manual fix"},
        )
    assert res.status_code == 200, res.text
    tx_id = res.json()["transaction_id"]

    async with session_factory() as db:
        rows = (await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "org.account.balance.adjust"
            )
        )).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == AuditOutcome.SUCCESS
        assert row.actor_user_id == seeded["admin_id"]
        assert row.target_org_id == seeded["org_id"]
        assert row.detail["account_id"] == seeded["account_id"]
        assert row.detail["account_name"] == "Primary"
        assert row.detail["delta"] == "50.00"
        assert row.detail["old_balance"] == "100.00"
        assert row.detail["new_balance"] == "150.00"
        assert row.detail["reason"] == "manual fix"
        assert row.detail["generated_transaction_id"] == tx_id


@pytest.mark.asyncio
async def test_adjust_audit_failure_rolls_back_balance(
    session_factory, seeded, monkeypatch
):
    """Force ``add_audit_event_to_session`` to raise inside the begin_nested
    block and verify the balance write rolls back (atomic write).
    """
    from app.services import audit_service as audit_mod

    def raising_add(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(audit_mod, "add_audit_event_to_session", raising_add)

    async with session_factory() as db:
        with pytest.raises(RuntimeError):
            await adjust_account_balance(
                db,
                seeded["org_id"],
                seeded["account_id"],
                target_balance=Decimal("150.00"),
                reason=None,
                actor_user_id=seeded["admin_id"],
                actor_email="admin@test.io",
                actor_org_name="Test Org",
                request_id=None,
                ip_address=None,
            )

    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        assert acct.balance == Decimal("100.00")
        assert (
            (await db.execute(select(Transaction))).scalars().all() == []
        )


# ── 16. Toggle endpoint audit row ────────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_endpoint_writes_audit_row(session_factory, seeded):
    """Drive the settings toggle through its router and verify the audit
    event lands. Exercised via the in-process TestClient against the
    settings router.
    """
    from app.routers.settings import router as settings_router

    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        async with session_factory() as db:
            return (await db.execute(
                select(User).where(User.role == Role.ADMIN)
            )).scalar_one()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(settings_router)

    with TestClient(app) as client:
        res = client.put(
            "/api/v1/settings/manual-balance-adjustment",
            json={"enabled": False},
        )
    assert res.status_code == 200, res.text
    assert res.json() == {"enabled": False}

    async with session_factory() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == seeded["org_id"])
        )).scalar_one()
        assert org.allow_manual_balance_adjustment is False

        rows = (await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type
                == "org.config.allow_manual_balance_adjustment.set"
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].detail == {"old": True, "new": False}


# ── 17. Reconcile invariant ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_consistent_after_adjustment(session_factory, seeded):
    """Stored balance must equal the sum of settled rows including the
    adjustment row. This is what the architect spec calls out: the
    adjustment is a real settled tx for reconcile purposes.

    We zero out the seeded starting balance first (the fixture sets
    balance=100 directly without a corresponding transaction, which
    would already break reconcile_account on its own — orthogonal to
    Track E). With a clean balance of 0, the only settled row is the
    adjustment, and stored should equal the sum.
    """
    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        acct.balance = Decimal("0.00")
        await db.commit()

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "50.00"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        acct = (await db.execute(
            select(Account).where(Account.id == seeded["account_id"])
        )).scalar_one()
        stored, computed, consistent = await reconcile_account(
            db, seeded["org_id"], acct
        )
        assert stored == Decimal("50.00")
        # Adjustment generated an INCOME of 50; reconcile sums settled
        # INCOME minus EXPENSE and includes the adjustment row (the
        # spec is explicit: do NOT exclude adjustments from reconcile).
        assert computed == Decimal("50.00")
        assert consistent is True


# ── 18. Excluded from reportable filter ──────────────────────────────────


@pytest.mark.asyncio
async def test_adjustment_excluded_from_reportable_filter(
    session_factory, seeded
):
    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        rows = (await db.execute(
            select(Transaction).where(reportable_transaction_filter())
        )).scalars().all()
        assert rows == []

        adj = (await db.execute(select(Transaction))).scalar_one()
        assert is_reportable_transaction(adj) is False


# ── 19-20. Excluded from transfer-pair detection ─────────────────────────


@pytest.mark.asyncio
async def test_adjustment_excluded_from_match_candidates(
    session_factory, seeded
):
    """Create an adjustment row, then call find_match_candidates with
    a query shape that would otherwise match it. Should return empty.
    """
    import datetime

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},  # creates INCOME 50 row
        )

    # Seed a second account so account_id_excluded != adjustment account.
    async with session_factory() as db:
        at = (await db.execute(
            select(AccountType).where(AccountType.org_id == seeded["org_id"])
        )).scalar_one()
        other = Account(
            org_id=seeded["org_id"],
            account_type_id=at.id,
            name="Other",
            balance=Decimal("0.00"),
            currency="EUR",
        )
        db.add(other)
        await db.commit()
        other_id = other.id

    async with session_factory() as db:
        candidates = await find_match_candidates(
            db,
            seeded["org_id"],
            source_type=TransactionType.EXPENSE,
            amount=Decimal("50.00"),
            account_id_excluded=other_id,
            date=datetime.date.today(),
            currency="EUR",
        )
        assert candidates == []


@pytest.mark.asyncio
async def test_adjustment_excluded_from_duplicate_of_linked_leg(
    session_factory, seeded
):
    """Adjustment rows are never linked, but the explicit filter is an
    invariant assertion. Ensure the query doesn't surface it.
    """
    import datetime

    app = _make_app(session_factory, _resolver_for(Role.ADMIN))
    with TestClient(app) as client:
        client.post(
            f"/api/v1/accounts/{seeded['account_id']}/adjust-balance",
            json={"target_balance": "150.00"},
        )

    async with session_factory() as db:
        rows = await find_duplicate_of_linked_leg(
            db,
            seeded["org_id"],
            account_id=seeded["account_id"],
            amount=Decimal("50.00"),
            type=TransactionType.INCOME,
            date=datetime.date.today(),
            currency="EUR",
        )
        assert rows == []


# ── 21-24. Standard CRUD guards ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_transaction_rejects_adjustment_row(
    session_factory, seeded
):
    async with session_factory() as db:
        tx, _, _, _ = await adjust_account_balance(
            db,
            seeded["org_id"],
            seeded["account_id"],
            target_balance=Decimal("150.00"),
            reason=None,
            actor_user_id=seeded["admin_id"],
            actor_email="admin@test.io",
            actor_org_name="Test Org",
            request_id=None,
            ip_address=None,
        )
        tx_id = tx.id

    async with session_factory() as db:
        with pytest.raises(ValidationError) as exc:
            await update_transaction(
                db,
                seeded["org_id"],
                tx_id,
                TransactionUpdate(description="hacked"),
            )
        assert "cannot be edited" in str(exc.value)


@pytest.mark.asyncio
async def test_delete_transaction_rejects_adjustment_row(
    session_factory, seeded
):
    async with session_factory() as db:
        tx, _, _, _ = await adjust_account_balance(
            db,
            seeded["org_id"],
            seeded["account_id"],
            target_balance=Decimal("150.00"),
            reason=None,
            actor_user_id=seeded["admin_id"],
            actor_email="admin@test.io",
            actor_org_name="Test Org",
            request_id=None,
            ip_address=None,
        )
        tx_id = tx.id

    async with session_factory() as db:
        with pytest.raises(ValidationError) as exc:
            await delete_transaction(db, seeded["org_id"], tx_id)
        assert "cannot be deleted" in str(exc.value)


@pytest.mark.asyncio
async def test_bulk_delete_skips_adjustment_rows(session_factory, seeded):
    """Architect override: bulk delete is silent. The id surfaces in
    skipped_ids; deleted_count counts only rows actually removed.
    """
    import datetime

    from app.schemas.transaction import TransactionCreate
    from app.services.transaction_service import _create_transaction_no_commit

    async with session_factory() as db:
        # Adjustment row.
        adj, _, _, _ = await adjust_account_balance(
            db,
            seeded["org_id"],
            seeded["account_id"],
            target_balance=Decimal("150.00"),
            reason=None,
            actor_user_id=seeded["admin_id"],
            actor_email="admin@test.io",
            actor_org_name="Test Org",
            request_id=None,
            ip_address=None,
        )
        adj_id = adj.id

    # Regular tx.
    async with session_factory() as db:
        regular = await _create_transaction_no_commit(
            db,
            seeded["org_id"],
            TransactionCreate(
                account_id=seeded["account_id"],
                category_id=seeded["category_id"],
                description="Regular",
                amount=Decimal("10.00"),
                type="expense",
                status="settled",
                date=datetime.date.today(),
            ),
        )
        regular_id = regular.id
        await db.commit()

    async with session_factory() as db:
        deleted, skipped = await bulk_delete_transactions(
            db, seeded["org_id"], [adj_id, regular_id]
        )
        assert deleted == 1
        assert adj_id in skipped
        assert regular_id not in skipped

    async with session_factory() as db:
        rows = (await db.execute(
            select(Transaction).where(Transaction.org_id == seeded["org_id"])
        )).scalars().all()
        # Adjustment row survives, regular row is gone.
        assert {r.id for r in rows} == {adj_id}


@pytest.mark.asyncio
async def test_promote_to_recurring_rejects_adjustment_row(
    session_factory, seeded
):
    import datetime

    async with session_factory() as db:
        tx, _, _, _ = await adjust_account_balance(
            db,
            seeded["org_id"],
            seeded["account_id"],
            target_balance=Decimal("150.00"),
            reason=None,
            actor_user_id=seeded["admin_id"],
            actor_email="admin@test.io",
            actor_org_name="Test Org",
            request_id=None,
            ip_address=None,
        )
        tx_id = tx.id

    async with session_factory() as db:
        with pytest.raises(ValidationError) as exc:
            await promote_to_recurring(
                db,
                seeded["org_id"],
                tx_id,
                PromoteToRecurringRequest(
                    frequency="monthly",
                    next_due_date=datetime.date.today() + datetime.timedelta(days=30),
                ),
            )
        assert "manual balance adjustment" in str(exc.value).lower()
