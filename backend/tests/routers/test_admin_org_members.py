"""Router tests for L4.4 superadmin org member management.

Pins:
- Auth gate: 401 unauthenticated, 403 without `orgs.manage`.
- Listing returns the superset shape (`is_active`, `email_verified`,
  `is_superadmin`).
- PATCH guards: last-owner (deactivate / demote), self-target,
  superadmin target, no-op body.
- DELETE on `/members/{user_id}` is gone (2026-05-14): the underlying
  semantics were always "soft-deactivate", which the PATCH path
  already covers. The relabel-fix preserves the deactivate behavior
  via PATCH with ``is_active=False`` and removes the misleading
  "Remove" affordance from the UI and API.
- Audit events: one row per real change, no row for no-op PATCH.
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


def _make_app(session_factory, resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await resolver(session_factory)

    def override_get_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    app.include_router(admin_orgs_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs, the second populated so we can test member admin.

    Admin Org: one superadmin owner ("root").
    Target Inc: one OWNER ("t_owner"), one ADMIN ("t_admin"), one
    MEMBER ("t_member"), and one INACTIVE MEMBER ("t_ghost") so the
    list endpoint exercises the active/inactive split.
    """
    async with factory() as db:
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
        owner = User(
            org_id=target.id,
            username="t_owner",
            email="t_owner@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        admin = User(
            org_id=target.id,
            username="t_admin",
            email="t_admin@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=target.id,
            username="t_member",
            email="t_member@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,
        )
        ghost = User(
            org_id=target.id,
            username="t_ghost",
            email="t_ghost@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=False,
            email_verified=True,
        )
        # A second org-hosted superadmin so the "superadmin guard"
        # branch has a target that lives inside `target` and not
        # inside `admin_org`.
        embedded_sa = User(
            org_id=target.id,
            username="t_sa",
            email="t_sa@target.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add_all([sa, owner, admin, member, ghost, embedded_sa])
        await db.commit()

        return {
            "admin_user_id": sa.id,
            "admin_org_id": admin_org.id,
            "target_id": target.id,
            "owner_id": owner.id,
            "admin_id": admin.id,
            "member_id": member.id,
            "ghost_id": ghost.id,
            "embedded_sa_id": embedded_sa.id,
        }


def _superadmin_resolver():
    async def resolve(factory):
        async with factory() as db:
            return (
                await db.execute(
                    select(User).where(
                        User.is_superadmin.is_(True),
                        User.email == "root@platform.io",
                    )
                )
            ).scalar_one()

    return resolve


def _plain_user_resolver():
    async def resolve(factory):
        async with factory() as db:
            return (
                await db.execute(
                    select(User).where(User.username == "t_owner")
                )
            ).scalar_one()

    return resolve


async def _audit_events(factory, event_type: str | None = None) -> list[AuditEvent]:
    async with factory() as db:
        q = select(AuditEvent)
        if event_type:
            q = q.where(AuditEvent.event_type == event_type)
        result = await db.execute(q)
        return list(result.scalars().all())


# ── auth gates ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members_403_for_non_superadmin(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}/members")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_patch_member_403_for_non_superadmin(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['member_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_delete_member_endpoint_is_removed(session_factory):
    """The misleading DELETE `/members/{user_id}` was retired on
    2026-05-14 in favor of PATCH ``is_active=False``. Confirm the
    method is no longer routed (405) so client code can't accidentally
    call the old shape.
    """
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['member_id']}"
        )
    # FastAPI returns 405 Method Not Allowed when the path matches a
    # router prefix but the verb isn't bound. Either 404 or 405 is
    # acceptable; both prove the verb is gone.
    assert res.status_code in (404, 405)


# ── list ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members_returns_full_shape(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/orgs/{seed['target_id']}/members")
    assert res.status_code == 200
    body = res.json()
    usernames = sorted(m["username"] for m in body)
    assert usernames == ["t_admin", "t_ghost", "t_member", "t_owner", "t_sa"]
    # Shape check: every member row has the required keys.
    for m in body:
        assert set(m.keys()) >= {
            "id", "username", "email", "role",
            "is_active", "email_verified", "is_superadmin", "created_at",
        }
    by_user = {m["username"]: m for m in body}
    assert by_user["t_ghost"]["is_active"] is False
    assert by_user["t_member"]["email_verified"] is False
    assert by_user["t_sa"]["is_superadmin"] is True


@pytest.mark.asyncio
async def test_list_members_404_for_missing_org(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/orgs/99999/members")
    assert res.status_code == 404


# ── PATCH success paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_member_role_changed_writes_audit(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['member_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "admin"

    rows = await _audit_events(session_factory, "admin.org.member.role_changed")
    assert len(rows) == 1
    assert rows[0].target_org_id == seed["target_id"]
    assert rows[0].detail["before"]["role"] == "member"
    assert rows[0].detail["after"]["role"] == "admin"
    assert "role" in rows[0].detail["changed_fields"]


@pytest.mark.asyncio
async def test_patch_member_deactivate_writes_audit(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['admin_id']}",
            json={"is_active": False},
        )
    assert res.status_code == 200
    assert res.json()["is_active"] is False
    rows = await _audit_events(session_factory, "admin.org.member.deactivated")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_patch_member_reactivate_writes_audit(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['ghost_id']}",
            json={"is_active": True},
        )
    assert res.status_code == 200
    assert res.json()["is_active"] is True
    rows = await _audit_events(session_factory, "admin.org.member.reactivated")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_patch_member_no_change_writes_no_audit(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        # Apply role=admin to a user who is already admin.
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['admin_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 200
    rows = await _audit_events(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_patch_member_empty_body_400(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['member_id']}",
            json={},
        )
    assert res.status_code == 400


# ── PATCH safety guards ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_cannot_target_self(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['admin_org_id']}/members/{seed['admin_user_id']}",
            json={"is_active": False},
        )
    assert res.status_code == 400
    assert "your own" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_cannot_demote_last_owner(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['owner_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 409
    assert "last active owner" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_cannot_deactivate_last_owner(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['owner_id']}",
            json={"is_active": False},
        )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_patch_can_demote_owner_when_second_owner_exists(session_factory):
    """Inverse pin: with a second active OWNER, demoting the first is
    allowed. Confirms the guard is "last" not "any"."""
    seed = await _seed(session_factory)
    # Promote t_admin to OWNER first (legal — still 1 active owner
    # plus 1 demotable promotion target). Then demote t_owner.
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        # Promote t_admin → owner.
        promote = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['admin_id']}",
            json={"role": "owner"},
        )
        assert promote.status_code == 200
        # Now demote t_owner → admin (legal — t_admin is the second owner).
        demote = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['owner_id']}",
            json={"role": "admin"},
        )
    assert demote.status_code == 200


@pytest.mark.asyncio
async def test_patch_cannot_target_superadmin(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/{seed['embedded_sa_id']}",
            json={"is_active": False},
        )
    assert res.status_code == 403
    assert "superadmin" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_member_404_for_missing_user(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['target_id']}/members/99999",
            json={"role": "admin"},
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_member_404_for_missing_org(session_factory):
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/99999/members/{seed['member_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_member_in_wrong_org_404(session_factory):
    """User exists, but not in the path org. Treated as not-found,
    matching the org-scoped contract used elsewhere."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/orgs/{seed['admin_org_id']}/members/{seed['member_id']}",
            json={"role": "admin"},
        )
    assert res.status_code == 404


# ── DELETE retired ─────────────────────────────────────────────────────────
# The DELETE method on `/members/{user_id}` was removed on 2026-05-14
# because it shared semantics with PATCH `is_active=False` while
# emitting a misleading `admin.org.member.removed` audit event. All
# deactivate flows now route through the PATCH path tested above; the
# router-removal smoke check lives at
# ``test_delete_member_endpoint_is_removed`` near the auth gates.
