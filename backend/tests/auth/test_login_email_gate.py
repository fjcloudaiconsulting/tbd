"""L1.8 — block local login until email is verified.

Pins the new gate on `POST /api/v1/auth/login` and the unauthenticated
sibling endpoint `POST /api/v1/auth/resend-verification-public` that the
login screen calls to re-trigger the verification email when the user
can't get an access token yet.
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
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


PASSWORD = "S3cret-Pass!"


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
    """Each test starts with an empty rate-limiter state. Several tests in
    this file hit `/login` and `/resend-verification-public`, both of
    which carry rate limits; without resetting they bleed across tests."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def stub_send_verification_email(monkeypatch):
    """Capture verification-email sends without hitting the network. Tests
    that need to assert on sent mail re-bind the captured list."""
    sent: list[tuple[str, str]] = []

    async def fake_send(email: str, token: str) -> None:
        sent.append((email, token))

    monkeypatch.setattr(auth_module, "send_verification_email", fake_send)
    return sent


def make_app(session_factory) -> FastAPI:
    app = FastAPI()
    # slowapi requires the limiter on app.state plus the rate-limit handler
    # for any decorated route to function under TestClient.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router)
    return app


async def _seed_user(
    factory,
    *,
    email_verified: bool,
    is_active: bool = True,
    username: str = "alice",
    email: str = "alice@example.com",
) -> int:
    async with factory() as db:
        org = Organization(name="org", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=is_active,
            email_verified=email_verified,
        )
        db.add(user)
        await db.commit()
        return user.id


@pytest.mark.asyncio
async def test_login_blocks_unverified_local_user_with_structured_detail(
    session_factory,
):
    await _seed_user(session_factory, email_verified=False)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 403
    assert res.json()["detail"] == {
        "code": "email_not_verified",
        "message": "Please verify your email to sign in.",
    }


@pytest.mark.asyncio
async def test_login_succeeds_for_verified_user(session_factory):
    await _seed_user(session_factory, email_verified=True)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 200
    assert "access_token" in res.json()


@pytest.mark.asyncio
async def test_login_deactivated_takes_priority_over_unverified(session_factory):
    """Deactivated wins over unverified — preserves precedence with the
    pre-L1.8 handler."""
    await _seed_user(session_factory, email_verified=False, is_active=False)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": PASSWORD},
        )

    assert res.status_code == 403
    assert res.json()["detail"] == "Account is deactivated"


@pytest.mark.asyncio
async def test_resend_public_sends_for_unverified_user(
    session_factory, stub_send_verification_email
):
    await _seed_user(session_factory, email_verified=False)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/resend-verification-public",
            json={"login": "alice"},
        )

    assert res.status_code == 200
    assert len(stub_send_verification_email) == 1
    assert stub_send_verification_email[0][0] == "alice@example.com"


@pytest.mark.asyncio
async def test_resend_public_no_op_for_verified_user(
    session_factory, stub_send_verification_email
):
    await _seed_user(session_factory, email_verified=True)

    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/resend-verification-public",
            json={"login": "alice@example.com"},
        )

    # No enumeration — same 200 body whether the user exists, is verified,
    # or doesn't exist.
    assert res.status_code == 200
    assert stub_send_verification_email == []


@pytest.mark.asyncio
async def test_resend_public_no_op_for_unknown_login(
    session_factory, stub_send_verification_email
):
    app = make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/resend-verification-public",
            json={"login": "ghost@example.com"},
        )

    assert res.status_code == 200
    assert stub_send_verification_email == []


@pytest.mark.asyncio
async def test_resend_public_rate_limit(
    session_factory, stub_send_verification_email
):
    """Limit is `3/hour` per IP — fourth call within the window returns 429."""
    await _seed_user(session_factory, email_verified=False)

    app = make_app(session_factory)
    with TestClient(app) as client:
        for _ in range(3):
            ok = client.post(
                "/api/v1/auth/resend-verification-public",
                json={"login": "alice"},
            )
            assert ok.status_code == 200
        throttled = client.post(
            "/api/v1/auth/resend-verification-public",
            json={"login": "alice"},
        )

    assert throttled.status_code == 429
