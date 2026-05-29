"""Regression tests for the org-delete cascade bugs fixed in this branch.

Bug 1: import_batches was missing from wipe_org_data, causing
IntegrityError 1451 when an org had any import_batch rows
(import_batches.account_id → accounts.id with no ON DELETE CASCADE).

Bug 2: the except block in delete_org accessed current_user.id /
current_user.email after db.rollback(), which expires ORM objects on an
async session → MissingGreenlet → secondary exception swallows the audit
write → no failure row in audit_events.

Test 1 (cascade with import_batches): proves Bug 1 is fixed — delete
succeeds and all rows are gone.

Test 2 (failure path writes audit row): proves Bug 2 is fixed — when
delete_org_cascade raises, the response is a structured JSON 500 AND a
failure audit row exists.
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import patch, AsyncMock

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
from app.models import Base
from app.models.account import Account, AccountType
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportSourceFormat
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.routers.admin_orgs import router as admin_orgs_router
from app.security import hash_password


# ── fixtures ──────────────────────────────────────────────────────────────


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


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    def override_session_factory():
        # Hand the test's in-memory factory to the audit recorder so it
        # writes into the same SQLite the test reads from.
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_orgs_router)
    return app


def _superadmin_resolver():
    async def resolve(sf):
        async with sf() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


async def _seed_with_import_batch(factory) -> dict:
    """Two orgs: Admin (superadmin) and Target (with an account +
    import_batch). Returns ids needed by the tests."""
    async with factory() as db:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()

        sa = User(
            org_id=admin_org.id,
            username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        target_owner = User(
            org_id=target.id,
            username="t_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([sa, target_owner])
        await db.commit()

        target_sub = Subscription(
            org_id=target.id,
            plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_end=datetime.date.today() + datetime.timedelta(days=14),
        )
        admin_sub = Subscription(
            org_id=admin_org.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add_all([target_sub, admin_sub])
        await db.commit()

        # Create an account type + account for the target org.
        atype = AccountType(
            org_id=target.id,
            name="Checking",
            slug="checking-target",
        )
        db.add(atype)
        await db.commit()

        account = Account(
            org_id=target.id,
            account_type_id=atype.id,
            name="Main",
            balance=Decimal("500.00"),
        )
        db.add(account)
        await db.commit()

        # Create an import_batch referencing the account. This is the
        # row that used to cause IntegrityError 1451 on org delete.
        batch = ImportBatch(
            org_id=target.id,
            account_id=account.id,
            source_format=ImportSourceFormat.CSV,
            file_name="jan.csv",
            created_by_user_id=target_owner.id,
            status=ImportBatchStatus.CLOSED,
        )
        db.add(batch)
        await db.commit()

        return {
            "admin_user_id": sa.id,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "target_name": target.name,
            "account_id": account.id,
            "batch_id": batch.id,
        }


# ── Test 1: cascade succeeds with import_batches present ──────────────────


@pytest.mark.asyncio
async def test_delete_org_cascade_clears_import_batches(session_factory):
    """Bug 1 regression: org delete with an import_batch row must not
    raise IntegrityError 1451. After delete, both import_batches and
    accounts rows for the org are gone and the audit row is success.
    """
    seed = await _seed_with_import_batch(session_factory)
    app = make_app(session_factory, _superadmin_resolver())

    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/api/v1/admin/orgs/{seed['target_id']}",
            json={"confirm_name": seed["target_name"]},
        )

    assert res.status_code == 200, f"expected 200, got {res.status_code}: {res.text}"
    body = res.json()
    assert body["deleted"]["import_batches"] >= 1
    assert body["deleted"]["accounts"] >= 1
    assert body["deleted"]["organizations"] == 1

    # Confirm rows are gone at the DB level.
    async with session_factory() as db:
        remaining_batches = (
            await db.execute(
                select(ImportBatch).where(ImportBatch.org_id == seed["target_id"])
            )
        ).scalars().all()
        assert remaining_batches == [], (
            "import_batch rows survived the org delete — cascade is still broken"
        )

        remaining_accounts = (
            await db.execute(
                select(Account).where(Account.org_id == seed["target_id"])
            )
        ).scalars().all()
        assert remaining_accounts == [], (
            "account rows survived the org delete"
        )

    # Audit row must exist with outcome=success.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.delete",
                    AuditEvent.outcome == AuditOutcome.SUCCESS,
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.detail["snapshot"]["org_id"] == seed["target_id"]
    assert row.detail["deleted_rows_by_table"]["import_batches"] >= 1


# ── Test 2: failure path returns structured JSON + writes audit row ────────


@pytest.mark.asyncio
async def test_delete_org_failure_writes_audit_and_returns_json(session_factory):
    """Bug 2 regression: when delete_org_cascade raises, the except
    block must NOT access expired ORM attributes (MissingGreenlet).
    The response must be a structured JSON 500 (not plain text), and a
    failure audit row must exist in audit_events.

    Without the Bug 2 fix the secondary MissingGreenlet swallows
    record_audit_event, no failure row is written, and Uvicorn returns
    raw text "Internal Server Error" instead of JSON.
    """
    seed = await _seed_with_import_batch(session_factory)
    app = make_app(session_factory, _superadmin_resolver())

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated cascade failure for Bug 2")

    with patch(
        "app.routers.admin_orgs.admin_orgs_service.delete_org_cascade",
        side_effect=boom,
    ):
        with TestClient(app) as client:
            res = client.request(
                "DELETE",
                f"/api/v1/admin/orgs/{seed['target_id']}",
                json={"confirm_name": seed["target_name"]},
            )

    # Must be 500 with a JSON body — NOT plain text.
    assert res.status_code == 500
    content_type = res.headers.get("content-type", "")
    assert "application/json" in content_type, (
        f"expected JSON 500 response, got content-type={content_type!r}; "
        f"body={res.text!r}. Without the Bug 2 fix, MissingGreenlet escapes "
        "the except block and Uvicorn returns plain text."
    )
    body = res.json()
    assert "detail" in body, f"no 'detail' key in 500 body: {body}"

    # The failure audit row must exist (written on the independent session).
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.delete.failed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1, (
        f"expected 1 failure audit row, found {len(rows)}. "
        "Without the Bug 2 fix, record_audit_event is never called because "
        "MissingGreenlet raises first in the except block."
    )
    row = rows[0]
    assert row.outcome == AuditOutcome.FAILURE
    assert row.target_org_id == seed["target_id"]
    assert row.detail is not None
    assert row.detail.get("error_type") == "RuntimeError"
    snapshot = row.detail.get("snapshot")
    assert snapshot is not None
    assert snapshot["org_id"] == seed["target_id"]
    assert snapshot["org_name"] == seed["target_name"]
    # deleted_by_email must be the snapshotted value, not a live ORM read.
    assert snapshot["deleted_by_email"] == "root@platform.io"

    # The target org is still present — the business txn rolled back.
    async with session_factory() as db:
        target = (
            await db.execute(
                select(Organization).where(Organization.id == seed["target_id"])
            )
        ).scalar_one_or_none()
    assert target is not None, "target org was deleted even though cascade raised"
