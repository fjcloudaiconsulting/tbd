"""End-to-end audit wiring tests (L4.7).

The point isn't to re-test admin behaviour (already covered in
``test_admin_orgs.py`` / ``test_org_data.py``) — it's to prove that
each call site we wired writes an ``audit_events`` row in addition
to its existing structlog event. Two important paths:

- A success: subscription override writes a `success` audit row.
- A FAILURE: when the org-cascade delete blows up mid-transaction,
  the business txn rolls back but an audit `failure` row must still
  exist (independent-session pattern).
"""
from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from unittest.mock import patch

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
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.subscription import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.models.user import Organization, Role, User
from app.routers.admin_orgs import router as admin_orgs_router
from app.security import hash_password


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
        # Hand the test's in-memory factory to the audit recorder so
        # it writes into the same SQLite the test reads from.
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_orgs_router)
    return app


async def _seed(factory) -> dict:
    async with factory() as db:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(
            org_id=admin_org.id, username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        db.add(sa)
        await db.commit()
        target_sub = Subscription(
            org_id=target.id, plan_id=plan.id,
            status=SubscriptionStatus.TRIALING,
            billing_interval=BillingInterval.MONTHLY,
            trial_end=datetime.date.today() + datetime.timedelta(days=14),
        )
        admin_sub = Subscription(
            org_id=admin_org.id, plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            billing_interval=BillingInterval.MONTHLY,
        )
        db.add_all([target_sub, admin_sub])
        await db.commit()
        return {
            "admin_user_id": sa.id,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "target_name": target.name,
        }


def _superadmin_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


@pytest.mark.asyncio
async def test_subscription_override_writes_audit(session_factory):
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/subscription",
            json={"status": "active"},
        )
    assert res.status_code == 200

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.subscription.override"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["admin_user_id"]
    assert row.actor_email == "root@platform.io"
    assert row.target_org_id == seed["target_id"]
    assert row.target_org_name == seed["target_name"]
    assert row.detail is not None
    assert "before" in row.detail and "after" in row.detail


@pytest.mark.asyncio
async def test_delete_failed_writes_audit(session_factory):
    """Patch delete_org_cascade to raise — verify a `failure` audit
    row exists even though the business txn rolled back. This is the
    whole point of writing audit on an independent session.
    """
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated cascade failure")

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
    assert res.status_code == 500

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.delete.failed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.FAILURE
    assert row.target_org_id == seed["target_id"]
    assert row.target_org_name == seed["target_name"]
    assert row.detail is not None
    assert row.detail.get("error_type") == "RuntimeError"

    # The target org is still present — the business txn was rolled
    # back. Sanity check: the audit row survived a rollback because
    # it was written on its OWN session.
    async with session_factory() as db:
        target = (
            await db.execute(
                select(Organization).where(Organization.id == seed["target_id"])
            )
        ).scalar_one_or_none()
    assert target is not None
