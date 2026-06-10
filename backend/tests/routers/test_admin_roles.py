"""Router tests for L4.8 /api/v1/admin/roles + /api/v1/admin/permissions.

Pins:
- Auth gate (roles.manage; non-superadmin gets 403).
- CRUD shapes match RoleDetailResponse.
- Frozen-row guards refuse PATCH and DELETE with 409.
- Slug uniqueness 409.
- Unknown permission key 422.
- Permission catalog returns grouped namespaces + flat key list.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.role import PlatformRole, RolePermission
from app.models.user import Organization, Role, User
from app.routers.admin_roles import router as admin_roles_router
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


def make_app(session_factory, current_user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await current_user_resolver(session_factory)

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_roles_router)
    return app


async def _seed_users(factory) -> dict:
    async with factory() as db:
        org = Organization(name="Platform", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        sa = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        plain = User(
            org_id=org.id,
            username="user",
            email="u@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
        )
        db.add_all([sa, plain])
        await db.commit()
        return {"org_id": org.id, "sa_id": sa.id, "plain_id": plain.id}


async def _seed_frozen_superadmin(factory) -> int:
    async with factory() as db:
        role = PlatformRole(
            slug="superadmin",
            name="Superadmin",
            is_system_frozen=True,
        )
        db.add(role)
        await db.flush()
        db.add(RolePermission(role_id=role.id, permission_key="admin.view"))
        db.add(RolePermission(role_id=role.id, permission_key="orgs.manage"))
        await db.commit()
        return role.id


def _superadmin_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(True)))
            ).scalar_one()
    return resolve


def _plain_user_resolver():
    async def resolve(session_factory):
        async with session_factory() as db:
            return (
                await db.execute(select(User).where(User.is_superadmin.is_(False)))
            ).scalar_one()
    return resolve


# ── auth gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_roles_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/roles")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_create_role_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/roles",
            json={"slug": "ops", "name": "Ops", "permissions": []},
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_permission_catalog_requires_superadmin(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _plain_user_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/permissions")
    assert res.status_code == 403


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_roles_returns_seeded_frozen(session_factory):
    await _seed_users(session_factory)
    await _seed_frozen_superadmin(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/roles")
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 1
    # ``total`` mirrors the other admin-table list envelopes so the FE
    # can reuse the shared <Pagination> component.
    assert body["total"] == 1
    item = body["items"][0]
    assert item["slug"] == "superadmin"
    assert item["is_system_frozen"] is True
    assert item["permission_count"] == 2


async def _seed_role(factory, *, slug, name, frozen=False, permissions=()):
    async with factory() as db:
        role = PlatformRole(slug=slug, name=name, is_system_frozen=frozen)
        db.add(role)
        await db.flush()
        for key in permissions:
            db.add(RolePermission(role_id=role.id, permission_key=key))
        await db.commit()
        return role.id


# ── server-side sort + pagination (shared list contract) ──────────────────


@pytest.mark.asyncio
async def test_list_roles_default_order_frozen_first(session_factory):
    await _seed_users(session_factory)
    await _seed_frozen_superadmin(session_factory)  # name "Superadmin"
    await _seed_role(session_factory, slug="alpha", name="Alpha")
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/roles")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    # Frozen pinned first regardless of name ("Superadmin" > "Alpha").
    assert [i["slug"] for i in body["items"]] == ["superadmin", "alpha"]


@pytest.mark.asyncio
async def test_list_roles_sort_name_desc(session_factory):
    await _seed_users(session_factory)
    await _seed_role(session_factory, slug="alpha", name="Alpha")
    await _seed_role(session_factory, slug="zeta", name="Zeta")
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/roles",
            params={"sort_by": "name", "sort_dir": "desc"},
        )
    assert res.status_code == 200
    names = [i["name"] for i in res.json()["items"]]
    assert names == ["Zeta", "Alpha"]


@pytest.mark.asyncio
async def test_list_roles_sort_permission_count_desc(session_factory):
    await _seed_users(session_factory)
    await _seed_role(
        session_factory, slug="few", name="Few", permissions=["admin.view"]
    )
    await _seed_role(
        session_factory,
        slug="many",
        name="Many",
        permissions=["admin.view", "orgs.view", "audit.view"],
    )
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/roles",
            params={"sort_by": "permission_count", "sort_dir": "desc"},
        )
    assert res.status_code == 200
    slugs = [i["slug"] for i in res.json()["items"]]
    assert slugs == ["many", "few"]


@pytest.mark.asyncio
async def test_list_roles_pagination_total_is_full_count(session_factory):
    await _seed_users(session_factory)
    for i in range(5):
        await _seed_role(session_factory, slug=f"role_{i}", name=f"Role {i}")
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/roles",
            params={"sort_by": "name", "sort_dir": "asc", "limit": 2, "offset": 0},
        )
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5
    assert [i["name"] for i in body["items"]] == ["Role 0", "Role 1"]


@pytest.mark.asyncio
async def test_list_roles_invalid_sort_by_returns_400(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/roles", params={"sort_by": "not_a_column"}
        )
    assert res.status_code == 400
    assert "invalid_sort_by" in res.json()["detail"]


@pytest.mark.asyncio
async def test_list_roles_invalid_sort_dir_returns_400(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(
            "/api/v1/admin/roles",
            params={"sort_by": "name", "sort_dir": "sideways"},
        )
    assert res.status_code == 400
    assert "invalid_sort_dir" in res.json()["detail"]


@pytest.mark.asyncio
async def test_create_role_persists(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/roles",
            json={
                "slug": "support",
                "name": "Support",
                "description": "Read-only ops",
                "permissions": ["admin.view", "orgs.view"],
            },
        )
    assert res.status_code == 201
    body = res.json()
    assert body["slug"] == "support"
    assert body["is_system_frozen"] is False
    assert sorted(body["permissions"]) == ["admin.view", "orgs.view"]


@pytest.mark.asyncio
async def test_get_role_returns_detail(session_factory):
    await _seed_users(session_factory)
    role_id = await _seed_frozen_superadmin(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get(f"/api/v1/admin/roles/{role_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == "superadmin"
    assert sorted(body["permissions"]) == ["admin.view", "orgs.manage"]


@pytest.mark.asyncio
async def test_get_role_404(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/roles/9999")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_update_role_patches_permissions(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/roles",
            json={"slug": "ops", "name": "Ops", "permissions": ["admin.view"]},
        ).json()
        res = client.patch(
            f"/api/v1/admin/roles/{created['id']}",
            json={
                "name": "Operations",
                "permissions": ["admin.view", "audit.view"],
            },
        )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Operations"
    assert sorted(body["permissions"]) == ["admin.view", "audit.view"]


@pytest.mark.asyncio
async def test_update_role_clears_description_with_explicit_null(session_factory):
    """PR #142 #3: PATCH with `description: null` actually clears.

    Previously the service collapsed `None` into "leave unchanged"; this
    pins that explicit null clears the stored value while omitting the
    field still leaves it untouched.
    """
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/roles",
            json={
                "slug": "ops",
                "name": "Ops",
                "description": "Original",
                "permissions": [],
            },
        ).json()
        # PATCH without description leaves it.
        res = client.patch(
            f"/api/v1/admin/roles/{created['id']}",
            json={"name": "Operations"},
        )
        assert res.status_code == 200
        assert res.json()["description"] == "Original"
        # PATCH with explicit null clears.
        res = client.patch(
            f"/api/v1/admin/roles/{created['id']}",
            json={"description": None},
        )
        assert res.status_code == 200
        assert res.json()["description"] is None
        # PATCH with non-null sets.
        res = client.patch(
            f"/api/v1/admin/roles/{created['id']}",
            json={"description": "New value"},
        )
        assert res.status_code == 200
        assert res.json()["description"] == "New value"


@pytest.mark.asyncio
async def test_delete_role(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/roles",
            json={"slug": "ops", "name": "Ops", "permissions": []},
        ).json()
        res = client.delete(f"/api/v1/admin/roles/{created['id']}")
    assert res.status_code == 204
    # Verify deleted.
    async with session_factory() as db:
        rows = (await db.execute(select(PlatformRole))).scalars().all()
    assert rows == []


# ── frozen-row guards ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_frozen_role_returns_409(session_factory):
    await _seed_users(session_factory)
    role_id = await _seed_frozen_superadmin(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.patch(
            f"/api/v1/admin/roles/{role_id}",
            json={"name": "Hacked", "permissions": []},
        )
    assert res.status_code == 409
    assert "frozen" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_delete_frozen_role_returns_409(session_factory):
    await _seed_users(session_factory)
    role_id = await _seed_frozen_superadmin(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.delete(f"/api/v1/admin/roles/{role_id}")
    assert res.status_code == 409


# ── validation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_role_unknown_permission_returns_422(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/roles",
            json={
                "slug": "ops",
                "name": "Ops",
                "permissions": ["admin.view", "definitely.unknown.key"],
            },
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_role_invalid_slug_returns_422(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/roles",
            json={"slug": "BadSlug", "name": "x", "permissions": []},
        )
    # Pydantic Field(pattern=...) raises 422 before reaching service.
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_role_duplicate_slug_returns_409(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        client.post(
            "/api/v1/admin/roles",
            json={"slug": "ops", "name": "Ops", "permissions": []},
        )
        res = client.post(
            "/api/v1/admin/roles",
            json={"slug": "ops", "name": "Ops Two", "permissions": []},
        )
    assert res.status_code == 409


# ── permission catalog ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_catalog_groups_by_namespace(session_factory):
    await _seed_users(session_factory)
    app = make_app(session_factory, _superadmin_resolver())
    with TestClient(app) as client:
        res = client.get("/api/v1/admin/permissions")
    assert res.status_code == 200
    body = res.json()
    assert "namespaces" in body
    assert "admin" in body["namespaces"]
    assert "admin.view" in body["namespaces"]["admin"]
    assert "roles.manage" in body["keys"]
    # Keys are sorted.
    assert body["keys"] == sorted(body["keys"])
