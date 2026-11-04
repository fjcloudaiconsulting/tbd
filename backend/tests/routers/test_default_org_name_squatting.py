"""DB-03: a pre-claimed default org name cannot block sign-up.

Org names are globally unique (case-insensitive). The default name a new
account gets is predictable from its username, so it carries a random suffix,
and a collision on the unique index retries with a fresh suffix a bounded
number of times. Covered on both paths that create an org with a default name:
password registration without ``org_name`` and the Google sign-up branch of the
callback. A name the caller chose answers 409 when taken, like a rename.

The fixture mirrors migration 034's SQLite index (UNIQUE on ``LOWER(name)``):
``Base.metadata`` does not carry it, so without it nothing here could collide.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.captcha import REASON_OK, CaptchaVerifyResult
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.middleware.request_context import RequestContextMiddleware
from app.models import Base
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password

PASSWORD = "correct-horse-battery-1"
DEFAULT_NAME_RE = re.compile(r"^(?P<user>.+)'s Organization [0-9a-f]{6}$")


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_organizations_name_normalized "
            "ON organizations (LOWER(name))"
        ))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
        # A user already exists, so no request here is the first-user bootstrap.
        org = Organization(name="Seed Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(User(org_id=org.id, username="seed", email="seed@example.com",
                    password_hash=hash_password(PASSWORD), role=Role.OWNER,
                    is_active=True, email_verified=True))
        await db.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def stub_side_effects(monkeypatch):
    async def _ok(token, remote_ip):
        return CaptchaVerifyResult(ok=True, reason=REASON_OK)

    async def _no_mail(email: str, token: str) -> None:
        return None

    monkeypatch.setattr(auth_module, "verify_captcha", _ok)
    monkeypatch.setattr(auth_module, "send_verification_email", _no_mail)
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(RequestContextMiddleware)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


async def _claim(factory, name: str) -> None:
    async with factory() as db:
        db.add(Organization(name=name, billing_cycle_day=1))
        await db.commit()


async def _org_of(factory, username: str) -> str | None:
    async with factory() as db:
        return await db.scalar(
            select(Organization.name)
            .join(User, User.org_id == Organization.id)
            .where(User.username == username)
        )


async def _org_count(factory) -> int:
    async with factory() as db:
        return int(await db.scalar(select(func.count()).select_from(Organization)))


def _names(*names: str):
    """Stand-in for ``_default_org_name``: yields ``names`` in order."""
    it = iter(names)
    calls: list[str] = []

    def _next(username: str) -> str:
        calls.append(username)
        return next(it)

    return _next, calls


def _register(client: TestClient, username: str, **extra: Any):
    return client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@example.com",
        "password": PASSWORD, "captcha_token": "ok", **extra,
    })


# ── the default name ─────────────────────────────────────────────────────────


def test_default_name_carries_a_random_suffix():
    """Wrong implementation: the bare ``"{username}'s Organization"``, which
    anyone can claim ahead of the account it belongs to."""
    names = {auth_module._default_org_name("alice") for _ in range(50)}
    assert len(names) == 50
    for name in names:
        match = DEFAULT_NAME_RE.match(name)
        assert match and match["user"] == "alice", name


# ── password registration ────────────────────────────────────────────────────


async def test_register_ignores_a_claimed_bare_default_name(session_factory):
    await _claim(session_factory, "alice's Organization")
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, "alice")
    assert res.status_code == 201, res.text
    assert DEFAULT_NAME_RE.match(await _org_of(session_factory, "alice"))


async def test_register_retries_when_the_suffixed_name_is_taken(session_factory, monkeypatch):
    """Wrong implementation: one attempt, so the collision is a 500."""
    await _claim(session_factory, "alice's Organization aaaaaa")
    fake, calls = _names("ALICE's organization AAAAAA", "alice's Organization bbbbbb")
    monkeypatch.setattr(auth_module, "_default_org_name", fake)
    before = await _org_count(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, "alice")
    assert res.status_code == 201, res.text
    assert await _org_of(session_factory, "alice") == "alice's Organization bbbbbb"
    assert calls == ["alice", "alice"]
    assert await _org_count(session_factory) == before + 1


async def test_register_retries_are_bounded(session_factory, monkeypatch):
    """Wrong implementation: an unbounded retry loop."""
    await _claim(session_factory, "taken")
    calls: list[str] = []

    def _always_taken(username: str) -> str:
        calls.append(username)
        return "taken"

    monkeypatch.setattr(auth_module, "_default_org_name", _always_taken)
    with TestClient(_make_app(session_factory)) as client:
        with pytest.raises(IntegrityError):
            _register(client, "alice")
    assert len(calls) == auth_module._DEFAULT_ORG_NAME_ATTEMPTS
    assert await _org_of(session_factory, "alice") is None


async def test_register_with_a_taken_chosen_name_answers_409(session_factory):
    """A name the caller typed is not rewritten: it answers 409, as a rename
    does, and nothing is created."""
    await _claim(session_factory, "Household")
    before = await _org_count(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, "alice", org_name="household")
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == "An organization with that name already exists"
    assert await _org_of(session_factory, "alice") is None
    assert await _org_count(session_factory) == before


async def test_register_with_a_free_chosen_name_keeps_it(session_factory):
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, "alice", org_name="Household")
    assert res.status_code == 201, res.text
    assert await _org_of(session_factory, "alice") == "Household"


# ── Google sign-up ───────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_google(monkeypatch, email: str) -> None:
    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *a: Any, **kw: Any) -> _FakeResponse:
            return _FakeResponse(200, {"access_token": "fake-google-token"})

        async def get(self, *a: Any, **kw: Any) -> _FakeResponse:
            return _FakeResponse(200, {"email": email, "verified_email": True,
                                       "given_name": "Bob", "family_name": "Smith"})

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


def _google_callback(client: TestClient):
    client.cookies.set("oauth_state", "matching-state")
    return client.get("/api/v1/auth/google/callback",
                      params={"code": "dummy", "state": "matching-state"},
                      follow_redirects=False)


async def test_google_signup_ignores_a_claimed_bare_default_name(session_factory, monkeypatch):
    """The finding's case: the default name of the next Google sign-up is
    claimed first, and that person's sign-in used to fail every time."""
    await _claim(session_factory, "bob.smith's Organization")
    _patch_google(monkeypatch, "bob@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _google_callback(client)
    assert res.status_code == 302, res.text
    assert "#token=" in res.headers["location"]
    name = await _org_of(session_factory, "bob.smith")
    assert name and DEFAULT_NAME_RE.match(name), name


async def test_google_signup_retries_when_the_suffixed_name_is_taken(session_factory, monkeypatch):
    await _claim(session_factory, "bob.smith's Organization aaaaaa")
    fake, calls = _names("bob.smith's Organization aaaaaa", "bob.smith's Organization bbbbbb")
    monkeypatch.setattr(auth_module, "_default_org_name", fake)
    _patch_google(monkeypatch, "bob@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _google_callback(client)
    assert res.status_code == 302, res.text
    assert "#token=" in res.headers["location"]
    assert await _org_of(session_factory, "bob.smith") == "bob.smith's Organization bbbbbb"
    assert calls == ["bob.smith", "bob.smith"]
