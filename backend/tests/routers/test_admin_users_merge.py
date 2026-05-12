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
from fastapi import Depends, FastAPI
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


# ── actor-snapshot regression coverage (post-#222) ────────────────────────
#
# In production, ``get_current_user`` resolves ``actor`` through the same
# ``AsyncSession`` the request handler uses as ``db``. SQLAlchemy expires
# every instance attached to a session on ``rollback()`` regardless of the
# ``expire_on_commit`` flag, so any later ``actor.id`` / ``actor.email``
# access triggers a lazy load — and in async that needs the greenlet
# context the audit-write does not provide, raising ``MissingGreenlet``
# and turning every error path into a 500. The router fix snapshots
# ``actor_id`` / ``actor_email`` into locals before the first commit or
# rollback. These tests share the actor's session with the request to
# mirror production and prove the snapshot keeps the audit-write path
# happy on the 400 / 409 / 200 branches.


def _make_app_shared_session(session_factory, actor_user_id: int) -> FastAPI:
    """Like ``_make_app`` but the actor is loaded through the SAME
    session that the request handler uses as ``db``. This reproduces
    production where ``get_current_user`` and the route share one
    session, so a ``db.rollback()`` expires ``actor`` in place."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    async def override_current_user(
        db: AsyncSession = Depends(get_db),
    ) -> User:
        # Pull the actor through the same session FastAPI will pass to
        # the route as ``db``. FastAPI caches sub-dependency results
        # per request by callable identity — depending on ``get_db``
        # (which is overridden to ``override_get_db``) shares the
        # cache key with the route's own ``Depends(get_db)``, so
        # both see the same AsyncSession. Matches production where
        # ``get_current_user`` and the route share one session.
        user = await db.get(User, actor_user_id)
        assert user is not None
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(admin_users_router)
    return app


@pytest.mark.asyncio
async def test_merge_same_user_returns_400_with_shared_session(
    session_factory,
) -> None:
    """Reproducer for the merged-#222 bug: same-user merge with a shared
    actor/request session must return 400, not 500 (MissingGreenlet)."""
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory,
        org_id=org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )

    app = _make_app_shared_session(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": actor_id, "target_user_id": actor_id},
        )
    assert res.status_code == 400, res.text
    assert "different" in res.json()["detail"].lower()

    # And the failure audit row landed with the snapshotted actor.
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.merge.failed"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_user_id == actor_id
        assert rows[0].actor_email == "root@x.io"
        assert rows[0].detail["reason"] == "validation"


@pytest.mark.asyncio
async def test_merge_last_active_owner_returns_409_with_shared_session(
    session_factory,
) -> None:
    """The last-active-owner guard fires ``ConflictError`` → 409. The
    rollback path must not crash on a lazy-loaded ``actor.id``, and the
    failure audit row must still carry the snapshotted actor."""
    # The actor sits in a DIFFERENT org so it does not count toward
    # the "active OWNER" tally of source's org. Superadmin short-
    # circuits the orgs.manage permission gate regardless of org
    # membership, so the request still hits the merge service.
    actor_org_id = await _seed_org(session_factory, name="Actor Org")
    actor_id = await _seed_user(
        session_factory,
        org_id=actor_org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )
    # Target org has exactly one active OWNER (``source``). Target
    # is a MEMBER, so it cannot preserve the invariant. Deleting
    # source would leave the org without an active owner → 409.
    target_org_id = await _seed_org(session_factory, name="Target Org")
    source_id = await _seed_user(
        session_factory, org_id=target_org_id, username="solo", email="solo@x.io"
    )
    target_id = await _seed_user(
        session_factory, org_id=target_org_id, username="tgt", email="tgt@x.io"
    )
    async with session_factory() as db:
        target = await db.get(User, target_id)
        assert target is not None
        target.role = Role.MEMBER
        await db.commit()

    app = _make_app_shared_session(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": source_id, "target_user_id": target_id},
        )
    assert res.status_code == 409, res.text
    assert "only active owner" in res.json()["detail"]

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.merge.failed"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_user_id == actor_id
        assert rows[0].actor_email == "root@x.io"
        assert rows[0].detail["reason"] == "conflict"


@pytest.mark.asyncio
async def test_merge_success_with_shared_session_writes_snapshotted_audit(
    session_factory,
) -> None:
    """Happy path with shared actor/request session. The success audit
    row must carry the snapshotted actor id/email even though the
    request-scoped commit fired between actor resolution and the
    audit-write call."""
    org_id = await _seed_org(session_factory)
    actor_id = await _seed_user(
        session_factory,
        org_id=org_id,
        username="root",
        email="root@x.io",
        is_superadmin=True,
    )
    # Owner that survives the merge (preserves last-active-owner
    # invariant on the source org).
    await _seed_user(
        session_factory, org_id=org_id, username="keeper", email="keeper@x.io"
    )
    s_id = await _seed_user(
        session_factory, org_id=org_id, username="s", email="s@x.io"
    )
    t_id = await _seed_user(
        session_factory, org_id=org_id, username="t", email="t@x.io"
    )

    app = _make_app_shared_session(session_factory, actor_user_id=actor_id)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/admin/users/merge",
            json={"source_user_id": s_id, "target_user_id": t_id},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        # Source deleted.
        assert (await db.scalar(select(User).where(User.id == s_id))) is None
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.user.merged"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_user_id == actor_id
        assert rows[0].actor_email == "root@x.io"
        assert rows[0].detail["source_user_id"] == s_id
        assert rows[0].detail["target_user_id"] == t_id
