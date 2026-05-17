"""Layer C: POST /api/v1/categories/restore-recommended.

Architect-approved Category Fallback design (post-L3.10). Re-runs the
``SYSTEM_CATEGORIES`` seed for the current org. Owner-only (gated by
``require_org_owner``). Idempotent (skips slugs that already exist).
Audited as ``org.categories.restored``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_current_user
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.category import Category, CategoryType, SYSTEM_CATEGORIES
from app.models.user import Organization, Role, User
from app.routers.categories import router as categories_router
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


def _make_app(session_factory, user_resolver) -> FastAPI:
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user() -> User:
        return await user_resolver(session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(categories_router)
    return app


async def _seed(factory) -> dict:
    """Three users in one org: owner / admin / member. Empty categories."""
    async with factory() as db:
        org = Organization(name="Restore Co", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        owner = User(
            org_id=org.id, username="owner", email="o@x.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        admin = User(
            org_id=org.id, username="admin", email="a@x.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.ADMIN, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        member = User(
            org_id=org.id, username="member", email="m@x.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.MEMBER, is_superadmin=False, is_active=True,
            email_verified=True,
        )
        db.add_all([owner, admin, member])
        await db.commit()
        return {
            "org_id": org.id,
            "owner_id": owner.id,
            "admin_id": admin.id,
            "member_id": member.id,
        }


def _expected_seed_row_count() -> int:
    """Number of rows the seed should add to an empty org: masters +
    children + the standalone system categories (Transfer + Credit
    Card Payment). Source for the standalone list is
    ``STANDALONE_SYSTEM_CATEGORIES`` in ``org_bootstrap_service`` so
    a future addition to that list cannot silently drift this test
    away from the production seed."""
    from app.services.org_bootstrap_service import STANDALONE_SYSTEM_CATEGORIES

    total = 0
    for m in SYSTEM_CATEGORIES:
        total += 1 + len(m.get("children", []))
    return total + len(STANDALONE_SYSTEM_CATEGORIES)


def _user_resolver(role: Role):
    async def resolver(factory):
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.role == role))
            ).scalar_one()
    return resolver


@pytest.mark.asyncio
async def test_owner_seeds_full_set_on_empty_org(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.OWNER))

    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 200
    body = res.json()
    expected = _expected_seed_row_count()
    assert body["created_count"] == expected

    # Categories persisted with is_system=True.
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(Category).where(
                Category.org_id == seed["org_id"],
                Category.is_system.is_(True),
            )
        )
        assert count == expected


@pytest.mark.asyncio
async def test_owner_restore_includes_standalone_categories(session_factory) -> None:
    """Architect feedback on PR #297: ``restore_recommended_categories``
    must match ``seed_org_defaults`` exactly. After PR #296 added the
    ``credit_card_payment`` standalone category to the seed, restore
    must include it too — otherwise new/reset orgs and existing-org
    Restore actions produce different recommended sets.

    Pin both standalone slugs explicitly so a future drift (someone
    adds a new standalone seed but forgets restore, or vice versa)
    fails this test loudly."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.OWNER))

    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 200

    async with session_factory() as db:
        restored_slugs = set(
            (await db.scalars(
                select(Category.slug).where(
                    Category.org_id == seed["org_id"],
                    Category.is_system.is_(True),
                )
            )).all()
        )
    assert "transfer" in restored_slugs, (
        "restore must seed the Transfer system category"
    )
    assert "credit_card_payment" in restored_slugs, (
        "restore must seed the Credit Card Payment system category "
        "(parity with seed_org_defaults — PR #296 added this row)"
    )


@pytest.mark.asyncio
async def test_owner_second_call_is_idempotent(session_factory) -> None:
    """Running restore twice yields the same final state and second call
    returns ``created_count == 0``."""
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.OWNER))

    with TestClient(app) as client:
        first = client.post("/api/v1/categories/restore-recommended")
        second = client.post("/api/v1/categories/restore-recommended")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created_count"] == _expected_seed_row_count()
    assert second.json()["created_count"] == 0

    async with session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(Category).where(
                Category.org_id == seed["org_id"],
                Category.is_system.is_(True),
            )
        )
        assert count == _expected_seed_row_count()


@pytest.mark.asyncio
async def test_owner_preserves_user_renamed_system_category(
    session_factory,
) -> None:
    """If the user renamed a system slug (kept is_system=True), restore
    leaves the row alone — it does not reset the name."""
    seed = await _seed(session_factory)
    async with session_factory() as db:
        db.add(Category(
            org_id=seed["org_id"], name="My Income",
            slug="income", is_system=True, type=CategoryType.INCOME,
        ))
        await db.commit()

    app = _make_app(session_factory, _user_resolver(Role.OWNER))
    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 200

    async with session_factory() as db:
        row = await db.scalar(
            select(Category).where(
                Category.org_id == seed["org_id"],
                Category.slug == "income",
            )
        )
        assert row is not None
        assert row.name == "My Income"  # not overwritten


@pytest.mark.asyncio
async def test_admin_role_forbidden(session_factory) -> None:
    await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.ADMIN))
    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_member_role_forbidden(session_factory) -> None:
    await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.MEMBER))
    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_owner_call_writes_audit_event(session_factory) -> None:
    seed = await _seed(session_factory)
    app = _make_app(session_factory, _user_resolver(Role.OWNER))
    with TestClient(app) as client:
        client.post("/api/v1/categories/restore-recommended")

    async with session_factory() as db:
        events = (await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "org.categories.restored",
                AuditEvent.target_org_id == seed["org_id"],
            )
        )).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.outcome.value == "success"
    assert event.actor_user_id == seed["owner_id"]
    assert event.detail is not None
    assert event.detail["created_count"] == _expected_seed_row_count()


@pytest.mark.asyncio
async def test_org_isolation_does_not_touch_sibling_org(session_factory) -> None:
    """Restoring for Org A leaves Org B's categories untouched."""
    # Org A: empty, will run restore.
    seed_a = await _seed(session_factory)
    # Org B: another org with a single user-created category. The
    # restore for Org A must not create rows under Org B.
    async with session_factory() as db:
        other = Organization(name="Other Co", billing_cycle_day=1)
        db.add(other)
        await db.flush()
        db.add(Category(
            org_id=other.id, name="OtherCat", slug="othercat",
            type=CategoryType.EXPENSE, is_system=False,
        ))
        await db.commit()
        other_id = other.id

    app = _make_app(session_factory, _user_resolver(Role.OWNER))
    with TestClient(app) as client:
        res = client.post("/api/v1/categories/restore-recommended")
    assert res.status_code == 200

    async with session_factory() as db:
        # Org A got the full seed.
        count_a = await db.scalar(
            select(func.count()).select_from(Category).where(
                Category.org_id == seed_a["org_id"],
            )
        )
        assert count_a == _expected_seed_row_count()
        # Org B is untouched.
        count_b = await db.scalar(
            select(func.count()).select_from(Category).where(
                Category.org_id == other_id,
            )
        )
        assert count_b == 1
