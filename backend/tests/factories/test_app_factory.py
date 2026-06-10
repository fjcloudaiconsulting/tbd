"""Direct contract tests for ``make_test_app`` (the shared test-app factory).

These lock the ``current_user`` resolution contract documented in
``tests.factories.app``: a resolver must be a plain 0- or 1-positional-arg
callable (sync or async); the 1-arg form receives the ``session_factory``.

Every accepted ``current_user`` *shape* is exercised end-to-end through a
protected route:

  (a) a ``User`` instance
  (b) a zero-arg sync callable
  (c) a zero-arg async callable
  (d) a one-arg ``(factory)`` sync resolver
  (e) a one-arg ``(factory)`` async resolver
  (f) ``None`` → anonymous → protected route returns 401

The 7 router files migrated onto the factory only exercise shape (d)/(e); the
``User``-instance and anonymous paths are pinned here before the remaining
files migrate.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.deps import get_current_user
from app.models import Organization
from app.models.base import Base
from app.models.user import Role, User
from app.security import hash_password
from tests.factories import make_test_app


# A tiny protected router so the factory contract is tested in isolation,
# independent of any production router's seed requirements.
probe_router = APIRouter()


@probe_router.get("/whoami")
async def whoami(user: User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "username": user.username}


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

    async with factory() as db:
        org = Organization(name="Probe Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(
            User(
                org_id=org.id,
                username="probe",
                email="probe@example.com",
                password_hash=hash_password("x"),
                role=Role.OWNER,
                is_superadmin=True,
            )
        )
        await db.commit()

    try:
        yield factory
    finally:
        await engine.dispose()


async def _load_user(factory) -> User:
    async with factory() as db:
        return (
            await db.execute(select(User).where(User.username == "probe"))
        ).scalar_one()


@pytest.mark.asyncio
async def test_current_user_user_instance(session_factory):
    """Shape (a): a bare ``User`` instance is returned as-is."""
    user = await _load_user(session_factory)
    app = make_test_app(session_factory, routers=probe_router, current_user=user)

    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["username"] == "probe"


@pytest.mark.asyncio
async def test_current_user_zero_arg_sync(session_factory):
    """Shape (b): a zero-arg sync callable returning a ``User``."""
    user = await _load_user(session_factory)

    def resolver() -> User:
        return user

    app = make_test_app(session_factory, routers=probe_router, current_user=resolver)

    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["username"] == "probe"


@pytest.mark.asyncio
async def test_current_user_zero_arg_async(session_factory):
    """Shape (c): a zero-arg async callable returning a ``User``."""
    user = await _load_user(session_factory)

    async def resolver() -> User:
        return user

    app = make_test_app(session_factory, routers=probe_router, current_user=resolver)

    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["username"] == "probe"


@pytest.mark.asyncio
async def test_current_user_one_arg_sync(session_factory):
    """Shape (d): a one-arg ``(factory)`` sync resolver receives the factory."""

    def resolver(factory) -> User:
        # Prove the factory argument is the one we passed in.
        assert factory is session_factory
        # Resolve synchronously via a throwaway connection isn't trivial, so
        # just return a detached instance built off known seed data; the point
        # of this shape is the factory hand-off, exercised above.
        return User(
            id=999,
            org_id=1,
            username="sync-one-arg",
            email="s1@example.com",
            password_hash="x",
            role=Role.OWNER,
        )

    app = make_test_app(session_factory, routers=probe_router, current_user=resolver)

    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["username"] == "sync-one-arg"


@pytest.mark.asyncio
async def test_current_user_one_arg_async(session_factory):
    """Shape (e): a one-arg ``(factory)`` async resolver loads via the factory."""

    async def resolver(factory) -> User:
        assert factory is session_factory
        async with factory() as db:
            return (
                await db.execute(select(User).where(User.username == "probe"))
            ).scalar_one()

    app = make_test_app(session_factory, routers=probe_router, current_user=resolver)

    with TestClient(app) as client:
        resp = client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["username"] == "probe"


@pytest.mark.asyncio
async def test_current_user_none_is_anonymous_401(session_factory):
    """Shape (f): ``None`` leaves ``get_current_user`` un-overridden, so a
    protected route rejects an invalid/absent token with 401."""
    app = make_test_app(session_factory, routers=probe_router, current_user=None)
    assert get_current_user not in app.dependency_overrides

    with TestClient(app) as client:
        # A malformed bearer token passes the HTTPBearer scheme and reaches the
        # real get_current_user decode path, which returns 401.
        resp = client.get("/whoami", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_defaulted_and_varargs_resolvers_route_as_zero_arg(session_factory):
    """Hardened arity heuristic: ``def r(factory=None)`` and ``def r(*args)``
    have no *required positional* param, so they are called with zero args
    (not handed the session_factory)."""
    user = await _load_user(session_factory)

    def defaulted(factory=None) -> User:
        # If the factory were (wrongly) passed, this would be non-None.
        assert factory is None
        return user

    def varargs(*args) -> User:
        assert args == ()
        return user

    for resolver in (defaulted, varargs):
        app = make_test_app(
            session_factory, routers=probe_router, current_user=resolver
        )
        with TestClient(app) as client:
            resp = client.get("/whoami")
        assert resp.status_code == 200
        assert resp.json()["username"] == "probe"
