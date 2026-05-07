"""Reset-password handler regression coverage.

Pins:
  - A successful reset rotates the password hash and timestamps.
  - A successful reset flips `password_set=True`. Without this an SSO
    user who never set a password can use the reset-token flow to gain
    local credentials, but the UI keeps showing "Set a Password" and
    `/users/me/password` keeps treating the account as if no password
    has ever been chosen. (Finding 2 from the PR #138 review.)
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import create_password_reset_token, hash_password, verify_password


@pytest_asyncio.fixture
async def session_factory():
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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


def _make_app(session_factory):
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


async def _seed_user(session_factory, *, password_set: bool) -> int:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email="alice@acme.io",
            password_hash=hash_password("starting-password"),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=password_set,
        )
        db.add(user)
        await db.commit()
        return user.id


@pytest.mark.asyncio
async def test_reset_password_flips_password_set_true_for_sso_user(session_factory):
    """SSO user with `password_set=False` who resets via a valid token
    must end with `password_set=True`. Otherwise the UI keeps showing
    "Set a Password" and `/users/me/password` keeps thinking the
    account has no password chosen yet."""
    user_id = await _seed_user(session_factory, password_set=False)
    token = create_password_reset_token(user_id)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "fresh-strong-password"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.password_set is True
        assert verify_password("fresh-strong-password", user.password_hash)
        assert user.password_changed_at is not None
        assert user.sessions_invalidated_at is not None


@pytest.mark.asyncio
async def test_reset_password_keeps_password_set_true_for_classic_user(session_factory):
    """Existing classic users should remain `password_set=True` after a
    reset. Pins that the new write doesn't accidentally set False."""
    user_id = await _seed_user(session_factory, password_set=True)
    token = create_password_reset_token(user_id)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "another-strong-password"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        user = await db.get(User, user_id)
        assert user is not None
        assert user.password_set is True
        assert verify_password("another-strong-password", user.password_hash)
