"""End-to-end coverage of ``POST /api/v1/admin/users/merge``.

The merge service has its own unit tests in
``tests/services/test_user_merge_service.py``; this file covers the
router glue — auth gate, request body shape, success/failure status
codes, and audit-event emission.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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
        # audit-write session under StaticPool.
        async with session_factory() as db:
            user = await db.get(User, actor_user_id)
            assert user is not None
            return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_users_router)
    return app


async def _seed_user(
    factory,
    *,
    org_id: int,
    username: str,
    email: str,
    is_superadmin: bool = False,
) -> int:
    async with factory() as db:
        user = User(
            org_id=org_id,
            username=username,
            email=email,
            password_hash=hash_password("pw"),
            role=Role.OWNER,
            is_superadmin=is_superadmin,
            is_active=True,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_org(factory, *, name: str = "Acme") -> int:
    async with factory() as db:
        org = Organization(name=name, billing_cycle_day=1)
        db.add(org)
        await db.commit()
        return org.id


# ── auth gate ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_requires_orgs_manage(session_factory) -> None:
    """A non-superadmin without ``orgs.manage`` gets 403."""
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory, org_id=org_id, username="member", email="m@x.io"
    )
    s_id = await _seed_user(
        session_factory, org_id=org_id, username="s", email="s@x.io"
    )
    t_id = await _seed_user(
        session_factory, org_id=org_id, username="t", email="t@x.io"
    )

    app = _make_app(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": s_id, "target_user_id": t_id},
        )
    assert res.status_code == 403


# ── success path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_success_emits_audit_event(session_factory) -> None:
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory,
        org_id=org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )
    s_id = await _seed_user(
        session_factory, org_id=org_id, username="s", email="s@x.io"
    )
    t_id = await _seed_user(
        session_factory, org_id=org_id, username="t", email="t@x.io"
    )

    app = _make_app(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": s_id, "target_user_id": t_id},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source_user_id"] == s_id
    assert body["target_user_id"] == t_id
    assert "counts" in body

    # Source row is gone.
    async with session_factory() as db:
        assert (await db.scalar(select(User).where(User.id == s_id))) is None
        # Audit event landed.
        rows = (
            await db.execute(
                select(AuditEvent).where(AuditEvent.event_type == "admin.user.merged")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_user_id == actor_id
        assert rows[0].detail["source_user_id"] == s_id
        assert rows[0].detail["target_user_id"] == t_id


# ── failure paths ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_same_user_returns_400(session_factory) -> None:
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory,
        org_id=org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )

    app = _make_app(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": actor_id, "target_user_id": actor_id},
        )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_merge_missing_user_returns_404(session_factory) -> None:
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory,
        org_id=org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )
    t_id = await _seed_user(
        session_factory, org_id=org_id, username="t", email="t@x.io"
    )

    app = _make_app(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": 99999, "target_user_id": t_id},
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_merge_cross_org_returns_409(session_factory) -> None:
    org_a = await _seed_org(session_factory, name="A")
    org_b = await _seed_org(session_factory, name="B")
    actor_id = await _seed_user(
        session_factory,
        org_id=org_a,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )
    s_id = await _seed_user(
        session_factory, org_id=org_a, username="s", email="s@x.io"
    )
    t_id = await _seed_user(
        session_factory, org_id=org_b, username="t", email="t@x.io"
    )

    app = _make_app(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": s_id, "target_user_id": t_id},
        )
    assert res.status_code == 409
