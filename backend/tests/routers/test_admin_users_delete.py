"""End-to-end coverage of ``DELETE /api/v1/admin/users/{user_id}``.

The service has its own unit tests in
``tests/services/test_admin_users_service.py``; this file covers the
router glue — auth gate, structured 409 payloads, status codes, and
audit-event emission on both success and refusal branches.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

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
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.routers.admin_users import router as admin_users_router
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


def _make_app(session_factory, actor_user_id: int) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user() -> User:
        # Resolve the actor with a SEPARATE session so the user object
        # is not tied to the request session's connection. Otherwise a
        # rollback on the request session collides with the independent
        # audit-write session under StaticPool — same pattern as
        # ``test_admin_users_merge``.
        async with session_factory() as db:
            user = await db.get(User, actor_user_id)
            assert user is not None
            return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_users_router)
    return app


async def _seed(factory) -> dict:
    """One org. Superadmin actor, an inactive deletable target, an
    active member, an inactive superadmin (for the guard test), and
    a plain non-superadmin to confirm the auth gate denies them.
    """
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()

        sa = User(
            org_id=org.id, username="root", email="root@platform.io",
            password_hash=hash_password("pw"),
            role=Role.OWNER, is_superadmin=True, is_active=True,
            email_verified=True,
        )
        inactive = User(
            org_id=org.id, username="ghost", email="ghost@acme.io",
            password_hash=hash_password("pw"),
            role=Role.MEMBER, is_active=False, email_verified=True,
        )
        active = User(
            org_id=org.id, username="alice", email="alice@acme.io",
            password_hash=hash_password("pw"),
            role=Role.ADMIN, is_active=True, email_verified=True,
        )
        embedded_sa = User(
            org_id=org.id, username="sa2", email="sa2@acme.io",
            password_hash=hash_password("pw"),
            role=Role.ADMIN, is_superadmin=True, is_active=False,
            email_verified=True,
        )
        plain = User(
            org_id=org.id, username="bob", email="bob@acme.io",
            password_hash=hash_password("pw"),
            role=Role.MEMBER, is_active=True, email_verified=True,
        )
        db.add_all([sa, inactive, active, embedded_sa, plain])
        await db.commit()

        return {
            "org_id": org.id,
            "actor_id": sa.id,
            "inactive_id": inactive.id,
            "active_id": active.id,
            "embedded_sa_id": embedded_sa.id,
            "plain_id": plain.id,
        }


async def _audit_events(factory, event_type: str | None = None) -> list[AuditEvent]:
    async with factory() as db:
        q = select(AuditEvent)
        if event_type:
            q = q.where(AuditEvent.event_type == event_type)
        result = await db.execute(q)
        return list(result.scalars().all())


# ── auth gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_requires_users_delete_permission(session_factory) -> None:
    """A non-superadmin without ``users.delete`` gets 403."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["plain_id"])
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/users/{seed['inactive_id']}")
    assert res.status_code == 403


# ── precondition refusals ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_refuses_active_target_with_code(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["actor_id"])
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/users/{seed['active_id']}")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["code"] == "user_still_active"
    # The active user must still exist.
    async with session_factory() as db:
        target = await db.get(User, seed["active_id"])
        assert target is not None

    rows = await _audit_events(session_factory, "admin.user.delete.failed")
    assert len(rows) == 1
    assert rows[0].detail["code"] == "user_still_active"


@pytest.mark.asyncio
async def test_delete_user_refuses_superadmin_target_with_code(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["actor_id"])
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/users/{seed['embedded_sa_id']}")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["code"] == "user_is_superadmin"
    async with session_factory() as db:
        target = await db.get(User, seed["embedded_sa_id"])
        assert target is not None


@pytest.mark.asyncio
async def test_delete_user_refuses_self_target_with_code(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["actor_id"])
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/users/{seed['actor_id']}")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["code"] == "user_is_self"


# ── success path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_success_removes_row_and_emits_audit(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["actor_id"])
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/users/{seed['inactive_id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["deleted_user_id"] == seed["inactive_id"]

    async with session_factory() as db:
        gone = await db.get(User, seed["inactive_id"])
        assert gone is None

    rows = await _audit_events(session_factory, "admin.user.deleted")
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["target_user_id"] == seed["inactive_id"]
    assert detail["target_org_id"] == seed["org_id"]
    # The target_org_id snapshot lets the org's audit timeline still
    # see the deletion event after the User row is gone.
    assert rows[0].target_org_id == seed["org_id"]


# ── idempotency on already-deleted ────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_user_returns_404_when_target_missing(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, actor_user_id=seed["actor_id"])
    with TestClient(app) as client:
        res = client.delete("/api/v1/admin/users/99999")
    assert res.status_code == 404
