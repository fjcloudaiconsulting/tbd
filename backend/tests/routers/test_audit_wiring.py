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
from app.models.notification import Notification, NotificationCategory
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
        # Target org owner — receives the PR3 plan-changed
        # org_admin notification when the superadmin overrides the
        # plan_id. Without this, the broadcast would have zero
        # recipients and the notification assertion below would be
        # trivially satisfied.
        target_owner = User(
            org_id=target.id, username="target_owner",
            email="owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([sa, target_owner])
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
            "target_owner_id": target_owner.id,
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
async def test_subscription_override_with_plan_change_writes_plan_changed_audit(
    session_factory,
):
    """Audit gap closure PR2 (notification train).

    The generic ``admin.org.subscription.override`` row already covered
    any field change; PR3's notification dispatcher needs a clean
    ``admin.org.plan.changed`` trigger that fires ONLY when the plan
    actually changes (status flips, trial-end nudges shouldn't fan a
    "your plan changed" notification to every org member).

    This pins both rows on a plan-change PUT and only the generic row
    on a status-only PUT.
    """
    # Seed with an extra plan so we can switch to it.
    seed = await _seed(session_factory)
    async with session_factory() as db:
        pro = Plan(slug="pro", name="Pro")
        db.add(pro)
        await db.commit()
        pro_plan_id = pro.id

    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/subscription",
            json={"plan_id": pro_plan_id},
        )
    assert res.status_code == 200

    async with session_factory() as db:
        override_rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.subscription.override"
                )
            )
        ).scalars().all()
        plan_rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    # Generic compliance row still emitted.
    assert len(override_rows) == 1
    # AND the plan-specific row landed.
    assert len(plan_rows) == 1
    plan_row = plan_rows[0]
    assert plan_row.outcome == AuditOutcome.SUCCESS
    assert plan_row.actor_user_id == seed["admin_user_id"]
    assert plan_row.actor_email == "root@platform.io"
    assert plan_row.target_org_id == seed["target_id"]
    assert plan_row.target_org_name == seed["target_name"]
    assert plan_row.detail is not None
    # Old plan id is whatever the seed used (the free plan), new is `pro`.
    assert plan_row.detail["new_plan_id"] == pro_plan_id
    assert plan_row.detail["old_plan_id"] != pro_plan_id

    # PR3: the plan_changed notification fans out to every active
    # org_admin of the target org. The seed has exactly one owner
    # in the target org; that owner receives a row carrying the
    # audit_event_id for forensic correlation.
    async with session_factory() as db:
        plan_notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    assert len(plan_notifs) == 1
    notif = plan_notifs[0]
    assert notif.user_id == seed["target_owner_id"]
    assert notif.category == NotificationCategory.ORG_ADMIN
    assert "Pro" in notif.title
    assert "root@platform.io" in notif.body
    assert notif.audit_event_id == plan_row.id


@pytest.mark.asyncio
async def test_subscription_override_status_only_skips_plan_changed_audit(
    session_factory,
):
    """Status-only PUT must NOT emit ``admin.org.plan.changed`` —
    otherwise PR3's notification dispatcher would send a misleading
    "your plan changed" notification on every trial-status flip."""
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{seed['target_id']}/subscription",
            json={"status": "active"},
        )
    assert res.status_code == 200

    async with session_factory() as db:
        plan_rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.plan.changed"
                )
            )
        ).scalars().all()
    assert len(plan_rows) == 0, (
        "admin.org.plan.changed leaked on a status-only override — "
        "the conditional gate on 'plan_id' in before is broken"
    )


@pytest.mark.asyncio
async def test_delete_success_persists_audit_with_snapshot(session_factory):
    """PR-C / PR #139 #1: the org-delete success audit row must be
    durably persisted to ``audit_events`` (it used to be lost because
    the FK insert ran AFTER the org row was deleted, the FK violation
    was swallowed by record_audit_event, and the success only ever
    showed up in structlog).

    Pin:
    - row exists with outcome=SUCCESS and event_type=admin.org.delete
    - target_org_id is NULL (FK ON DELETE SET NULL fired in the same
      txn — the org is gone)
    - detail.snapshot.org_id preserves the original id even after the
      FK nulls
    - detail.deleted_rows_by_table is populated
    """
    seed = await _seed(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.request(
            "DELETE",
            f"/api/v1/admin/orgs/{seed['target_id']}",
            json={"confirm_name": seed["target_name"]},
        )
    assert res.status_code == 200

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.delete"
                )
            )
        ).scalars().all()
    assert len(rows) == 1, "success audit row missing — pre-PR-C bug"
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    # FK ON DELETE SET NULL fired when the org row was deleted in the
    # SAME transaction — target_org_id is now NULL on the audit row.
    assert row.target_org_id is None
    # Snapshot preserves the identity that the FK just dropped.
    assert row.detail is not None
    snapshot = row.detail.get("snapshot")
    assert snapshot is not None
    assert snapshot["org_id"] == seed["target_id"]
    assert snapshot["org_name"] == seed["target_name"]
    assert "member_count_at_delete" in snapshot
    assert snapshot["deleted_by_user_id"] == seed["admin_user_id"]
    assert snapshot["deleted_by_email"] == "root@platform.io"
    # Counts also recorded.
    assert "deleted_rows_by_table" in row.detail
    assert row.detail["deleted_rows_by_table"]["organizations"] == 1
    # Snapshot survives even though target_org_id is gone — the
    # org_name fallback column on the audit row is also still set.
    assert row.target_org_name == seed["target_name"]


@pytest.mark.asyncio
async def test_delete_rollback_drops_staged_audit_row(session_factory):
    """When delete_org_cascade raises after the success audit row has
    been staged in the same session, the rollback must take the
    staged audit row with it — no orphan ``admin.org.delete`` rows
    for deletes that didn't happen.

    The independent-session ``admin.org.delete.failed`` row is the
    only audit-table evidence in the rollback case; covered by
    test_delete_failed_writes_audit below.
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

    # The success row should NOT exist — the rollback dropped it.
    async with session_factory() as db:
        success_rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.delete",
                    AuditEvent.outcome == AuditOutcome.SUCCESS,
                )
            )
        ).scalars().all()
    assert len(success_rows) == 0, (
        "success audit row leaked despite rollback — pre-commit "
        "staging didn't roll back with the business txn"
    )


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
    # PR-C also embeds the org snapshot on the failure path so
    # ops can answer "which org failed to delete and what state was
    # it in" without reaching back to a structlog grep.
    snapshot = row.detail.get("snapshot")
    assert snapshot is not None
    assert snapshot["org_id"] == seed["target_id"]
    assert snapshot["org_name"] == seed["target_name"]
    assert snapshot["deleted_by_email"] == "root@platform.io"

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
