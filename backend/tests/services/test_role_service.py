"""Service-layer tests for L4.8 role_service.

Pins:
- ``create_role`` validates slug + permission keys, persists rows.
- ``update_role`` patches name/description/permissions, refuses on frozen.
- ``delete_role`` removes role + cascades to role_permissions, refuses on frozen.
- Slug uniqueness raises ConflictError.
- Unknown permission key raises ValidationError.
- Read path filters orphan permission keys.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.role import PlatformRole, RolePermission
from app.services import role_service
from app.services.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
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
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_frozen_superadmin(db: AsyncSession) -> int:
    role = PlatformRole(
        slug="superadmin",
        name="Superadmin",
        description="Frozen system role.",
        is_system_frozen=True,
    )
    db.add(role)
    await db.flush()
    db.add(RolePermission(role_id=role.id, permission_key="admin.view"))
    db.add(RolePermission(role_id=role.id, permission_key="orgs.manage"))
    await db.commit()
    return role.id


# ── create ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_role_persists_with_permissions(db_session):
    item = await role_service.create_role(
        db_session,
        slug="support",
        name="Support",
        description="Read-only ops",
        permissions=["admin.view", "orgs.view"],
    )
    await db_session.commit()

    assert item["slug"] == "support"
    assert item["name"] == "Support"
    assert item["description"] == "Read-only ops"
    assert item["is_system_frozen"] is False
    assert sorted(item["permissions"]) == ["admin.view", "orgs.view"]

    # Roundtrip via DB.
    rows = (await db_session.execute(select(RolePermission))).scalars().all()
    assert {r.permission_key for r in rows} == {"admin.view", "orgs.view"}


@pytest.mark.asyncio
async def test_create_role_rejects_invalid_slug(db_session):
    with pytest.raises(ValidationError):
        await role_service.create_role(
            db_session,
            slug="BadSlug",
            name="x",
            description=None,
            permissions=[],
        )
    with pytest.raises(ValidationError):
        await role_service.create_role(
            db_session,
            slug="ab",  # too short
            name="x",
            description=None,
            permissions=[],
        )


@pytest.mark.asyncio
async def test_create_role_rejects_unknown_permission(db_session):
    with pytest.raises(ValidationError):
        await role_service.create_role(
            db_session,
            slug="ops",
            name="Ops",
            description=None,
            permissions=["admin.view", "definitely.not.a.real.key"],
        )


@pytest.mark.asyncio
async def test_create_role_slug_uniqueness(db_session):
    await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=[],
    )
    await db_session.commit()
    with pytest.raises(ConflictError):
        await role_service.create_role(
            db_session,
            slug="ops",
            name="Ops Two",
            description=None,
            permissions=[],
        )


# ── update ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_role_replaces_permissions(db_session):
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=["admin.view"],
    )
    await db_session.commit()

    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        name="Operations",
        permissions=["admin.view", "audit.view"],
    )
    await db_session.commit()
    assert updated["name"] == "Operations"
    assert sorted(updated["permissions"]) == ["admin.view", "audit.view"]


@pytest.mark.asyncio
async def test_update_role_clears_permissions_with_empty_list(db_session):
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=["admin.view"],
    )
    await db_session.commit()
    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        permissions=[],
    )
    await db_session.commit()
    assert updated["permissions"] == []


@pytest.mark.asyncio
async def test_update_role_leaves_permissions_when_none(db_session):
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=["admin.view"],
    )
    await db_session.commit()
    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        name="Operations",
        permissions=None,
    )
    await db_session.commit()
    assert updated["permissions"] == ["admin.view"]


@pytest.mark.asyncio
async def test_update_role_clears_description_when_explicit_none(db_session):
    """PR #142 #3: passing description=None must clear the stored value.

    The router passes a sentinel via the schema's ``model_fields_set`` so
    "field omitted" stays a no-op while "field explicitly null" actually
    nulls out the column.
    """
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description="Original description",
        permissions=[],
    )
    await db_session.commit()

    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        description=None,
        clear_description=True,
    )
    await db_session.commit()
    assert updated["description"] is None


@pytest.mark.asyncio
async def test_update_role_leaves_description_when_omitted(db_session):
    """No description argument means leave stored value untouched."""
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description="Keep me",
        permissions=[],
    )
    await db_session.commit()

    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        name="Operations",
    )
    await db_session.commit()
    assert updated["description"] == "Keep me"


@pytest.mark.asyncio
async def test_update_role_sets_description_when_provided(db_session):
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=[],
    )
    await db_session.commit()

    updated = await role_service.update_role(
        db_session,
        role_id=item["id"],
        description="New description",
    )
    await db_session.commit()
    assert updated["description"] == "New description"


@pytest.mark.asyncio
async def test_update_role_refuses_frozen(db_session):
    role_id = await _seed_frozen_superadmin(db_session)
    with pytest.raises(ConflictError):
        await role_service.update_role(
            db_session,
            role_id=role_id,
            name="Hacked",
            permissions=[],
        )


@pytest.mark.asyncio
async def test_update_role_404_on_missing(db_session):
    with pytest.raises(NotFoundError):
        await role_service.update_role(
            db_session, role_id=99999, name="x"
        )


# ── delete ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_role_removes_role_and_permissions(db_session):
    item = await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=["admin.view"],
    )
    await db_session.commit()

    await role_service.delete_role(db_session, role_id=item["id"])
    await db_session.commit()

    rows = (
        await db_session.execute(select(PlatformRole))
    ).scalars().all()
    assert rows == []
    perms = (
        await db_session.execute(select(RolePermission))
    ).scalars().all()
    # FK CASCADE under SQLite when foreign_keys=ON.
    assert perms == []


@pytest.mark.asyncio
async def test_delete_role_refuses_frozen(db_session):
    role_id = await _seed_frozen_superadmin(db_session)
    with pytest.raises(ConflictError):
        await role_service.delete_role(db_session, role_id=role_id)


@pytest.mark.asyncio
async def test_delete_role_404_on_missing(db_session):
    with pytest.raises(NotFoundError):
        await role_service.delete_role(db_session, role_id=99999)


# ── read path: orphan filtering ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_role_filters_unknown_permission_keys(db_session):
    role = PlatformRole(
        slug="legacy",
        name="Legacy",
        is_system_frozen=False,
    )
    db_session.add(role)
    await db_session.flush()
    db_session.add(
        RolePermission(role_id=role.id, permission_key="admin.view")
    )
    db_session.add(
        RolePermission(role_id=role.id, permission_key="legacy.removed")
    )
    await db_session.commit()

    item = await role_service.get_role(db_session, role_id=role.id)
    assert item["permissions"] == ["admin.view"]


@pytest.mark.asyncio
async def test_list_roles_orders_frozen_first(db_session):
    role_id = await _seed_frozen_superadmin(db_session)
    await role_service.create_role(
        db_session,
        slug="ops",
        name="Ops",
        description=None,
        permissions=["admin.view"],
    )
    await db_session.commit()

    items, total = await role_service.list_roles(db_session)
    assert [r["slug"] for r in items][0] == "superadmin"
    assert total == 2


@pytest.mark.asyncio
async def test_list_roles_explicit_sort_by_name_desc(db_session):
    await _seed_frozen_superadmin(db_session)  # name "Superadmin"
    await role_service.create_role(
        db_session, slug="ops", name="Ops", description=None, permissions=[]
    )
    await role_service.create_role(
        db_session, slug="alpha", name="Alpha", description=None, permissions=[]
    )
    await db_session.commit()

    items, total = await role_service.list_roles(
        db_session, sort_by="name", sort_dir="desc"
    )
    assert [r["name"] for r in items] == ["Superadmin", "Ops", "Alpha"]
    assert total == 3


@pytest.mark.asyncio
async def test_list_roles_sort_by_permission_count(db_session):
    await role_service.create_role(
        db_session,
        slug="few",
        name="Few",
        description=None,
        permissions=["admin.view"],
    )
    await role_service.create_role(
        db_session,
        slug="many",
        name="Many",
        description=None,
        permissions=["admin.view", "orgs.view", "audit.view"],
    )
    await db_session.commit()

    items, _ = await role_service.list_roles(
        db_session, sort_by="permission_count", sort_dir="desc"
    )
    assert [r["slug"] for r in items] == ["many", "few"]


@pytest.mark.asyncio
async def test_list_roles_pagination_returns_full_total(db_session):
    for i in range(5):
        await role_service.create_role(
            db_session,
            slug=f"role_{i}",
            name=f"Role {i}",
            description=None,
            permissions=[],
        )
    await db_session.commit()

    items, total = await role_service.list_roles(
        db_session, sort_by="name", sort_dir="asc", limit=2, offset=0
    )
    assert len(items) == 2
    assert total == 5
    assert [r["name"] for r in items] == ["Role 0", "Role 1"]

    page2, total2 = await role_service.list_roles(
        db_session, sort_by="name", sort_dir="asc", limit=2, offset=2
    )
    assert [r["name"] for r in page2] == ["Role 2", "Role 3"]
    assert total2 == 5


@pytest.mark.asyncio
async def test_list_roles_invalid_sort_by_raises(db_session):
    with pytest.raises(ValidationError):
        await role_service.list_roles(db_session, sort_by="not_a_column")


@pytest.mark.asyncio
async def test_list_roles_sort_dir_without_sort_by_raises(db_session):
    with pytest.raises(ValidationError) as exc:
        await role_service.list_roles(db_session, sort_dir="asc")
    assert str(exc.value.detail) == "sort_dir_requires_sort_by"


@pytest.mark.asyncio
async def test_grouped_permissions_groups_by_namespace():
    grouped = role_service.grouped_permissions()
    assert "admin" in grouped
    assert "admin.view" in grouped["admin"]
    assert "roles" in grouped
    assert "roles.manage" in grouped["roles"]
