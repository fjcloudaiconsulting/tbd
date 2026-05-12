"""Regression tests — email is normalized and deduped across every
user-creation path. Covers the pre-launch bug where an early Google
SSO callback created a duplicate ``users`` row for an email that
already had a local-password user.

Each test boots an in-memory SQLite engine via the shared fixture
pattern from ``test_auth.py`` and exercises one create site.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import hash_password


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    """create_trial needs at least one active plan. Seed the bare
    minimum here so /register doesn't 500 in the test environment."""
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


# ── fixtures ───────────────────────────────────────────────────────────────


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


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


async def _seed_local_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    email: str,
    username: str = "flamarion",
    password: str = "starting-password-1",
) -> int:
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(password),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=False,  # local users start unverified
        )
        db.add(user)
        await db.commit()
        return user.id


async def _count_users(factory) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(User)) or 0


# ── /register dedupe ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_duplicate_email_rejected(session_factory) -> None:
    """Posting /register with an email that already exists → 409."""
    await _seed_local_user(session_factory, email="flamarion@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "flamarion@example.com",
                "password": "another-password-1",
            },
        )
    assert res.status_code == 409, res.text
    assert await _count_users(session_factory) == 1


@pytest.mark.asyncio
async def test_register_duplicate_email_different_case_rejected(session_factory) -> None:
    """Mixed-case duplicate is still rejected — Python normalization
    runs before the DB ever sees the value."""
    await _seed_local_user(session_factory, email="flamarion@example.com")
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "Flamarion@EXAMPLE.com",
                "password": "another-password-1",
            },
        )
    assert res.status_code == 409, res.text
    assert await _count_users(session_factory) == 1


@pytest.mark.asyncio
async def test_register_stores_normalized_email(session_factory) -> None:
    """A successful /register with whitespace + mixed case lands as
    the canonical form in the DB."""
    await _seed_default_plan(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "Flamarion@EXAMPLE.com",
                "password": "another-password-1",
            },
        )
    assert res.status_code in (200, 201), res.text
    async with session_factory() as db:
        u = await db.scalar(select(User).where(User.username == "newuser"))
        assert u is not None
        assert u.email == "flamarion@example.com"


# ── /google/callback merge ─────────────────────────────────────────────────


def _mock_google_callback_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    google_email: str,
    google_verified: bool = True,
    given_name: str = "Flam",
    family_name: str = "Arion",
) -> None:
    """Bypass the OAuth roundtrip — stub the two outbound HTTP calls so
    the callback runs with the supplied userinfo payload.

    Also stubs ``_validate_google_config`` to skip the env-var check
    and ``app_settings.app_url`` is left untouched (the redirect URL
    is constructed but the test follows redirects to None).
    """
    import httpx

    from app.routers import auth as auth_module

    class _FakeResponse:
        def __init__(self, status_code: int, json_payload: dict[str, Any]):
            self.status_code = status_code
            self._json = json_payload

        def json(self) -> dict[str, Any]:
            return self._json

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            # token endpoint
            return _FakeResponse(200, {"access_token": "fake-token"})

        async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            # userinfo endpoint
            return _FakeResponse(
                200,
                {
                    "email": google_email,
                    "verified_email": google_verified,
                    "given_name": given_name,
                    "family_name": family_name,
                    "picture": None,
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(auth_module, "_validate_google_config", lambda: None)


@pytest.mark.asyncio
async def test_google_callback_merges_into_existing_local_user(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug. Local user ``flamarion@example.com`` exists;
    Google SSO arrives for the same email. Post-state: ONE users row,
    not two; ``email_verified=True`` carried over from Google."""
    user_id = await _seed_local_user(
        session_factory, email="flamarion@example.com"
    )

    _mock_google_callback_dependencies(
        monkeypatch, google_email="flamarion@example.com"
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "test-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "test-state"},
            follow_redirects=False,
        )
    # The handler redirects to the frontend on success; either 302 or 200
    # depending on MFA. We don't care which — only that no new user landed.
    assert res.status_code in (200, 302), res.text
    assert await _count_users(session_factory) == 1

    async with session_factory() as db:
        u = await db.scalar(select(User).where(User.id == user_id))
        assert u is not None
        # email_verified is backfilled from Google's verified flag.
        assert u.email_verified is True


@pytest.mark.asyncio
async def test_google_callback_merges_even_when_google_email_has_different_case(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local user stored as ``flamarion@example.com``; Google returns
    ``Flamarion@Example.com`` (some IdPs preserve case). With the
    Python-side normalize, the lookup still matches."""
    user_id = await _seed_local_user(
        session_factory, email="flamarion@example.com"
    )

    _mock_google_callback_dependencies(
        monkeypatch, google_email="Flamarion@Example.com"
    )

    app = _make_app(session_factory)
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "test-state")
        res = client.get(
            "/api/v1/auth/google/callback",
            params={"code": "dummy", "state": "test-state"},
            follow_redirects=False,
        )
    assert res.status_code in (200, 302), res.text
    assert await _count_users(session_factory) == 1

    async with session_factory() as db:
        u = await db.scalar(select(User).where(User.id == user_id))
        assert u is not None
        # Email stays in the canonical (lowercased) form we stored at seed.
        assert u.email == "flamarion@example.com"
