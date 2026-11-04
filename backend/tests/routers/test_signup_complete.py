"""TBD-654: ``POST /api/v1/auth/signup/complete``, step 2 of email-first sign-up.

The signup token proves the inbox, so the account is created verified and
signed in. A name conflict answers 409 without burning the link; the link's
``jti`` is claimed only after the rows flush, so it is spent exactly once,
by the request that creates the account.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import state_db
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.middleware.request_context import RequestContextMiddleware
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan, Subscription
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import (
    SIGNUP_JTI_SCOPE,
    create_email_verification_token,
    create_signup_token,
    decode_signup_token,
    hash_password,
)

COMPLETE = "/api/v1/auth/signup/complete"
PASSWORD = "correct-horse-battery-1"


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migration 034's SQLite index; Base.metadata does not carry it.
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_organizations_name_normalized "
            "ON organizations (LOWER(name))"
        ))
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


async def _seed(factory, *, username: str = "seed", email: str = "seed@example.com") -> None:
    async with factory() as db:
        if await db.scalar(select(Plan.id)) is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
        org = Organization(name=f"{username} Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(User(
            org_id=org.id, username=username, email=email,
            password_hash=hash_password(PASSWORD), role=Role.OWNER,
            is_active=True, email_verified=True,
        ))
        await db.commit()


async def _count(factory, model) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


def _complete(client: TestClient, token: str, **overrides):
    body = {"token": token, "username": "newbie", "password": PASSWORD}
    body.update(overrides)
    return client.post(COMPLETE, json=body)


def _code(res) -> str:
    return res.json()["detail"]["code"]


# ── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_happy_path(session_factory):
    await _seed(session_factory)
    token = create_signup_token("New@Example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(
            client, token, first_name="Nia", last_name="Bee", org_name="Bee Household"
        )
        assert res.status_code == 200, res.text
        assert res.json()["access_token"]
        assert "refresh_token" in res.cookies
        me = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {res.json()['access_token']}"}
        )
        assert me.status_code == 200, me.text

    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.email == "new@example.com"))
        org = await db.get(Organization, user.org_id)
        sub = await db.scalar(select(Subscription).where(Subscription.org_id == user.org_id))
    assert user.username == "newbie" and user.first_name == "Nia"
    assert user.email_verified is True and user.password_set is True
    assert user.is_superadmin is False and user.is_founder is True
    assert user.role == Role.OWNER
    assert org.name == "Bee Household"
    assert sub is not None

    async with session_factory() as db:
        rows = list((await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "auth.register.success")
        )).scalars())
    assert len(rows) == 1
    assert rows[0].actor_user_id == user.id
    assert rows[0].target_org_name == "Bee Household"
    assert rows[0].detail == {
        "method": "email_link",
        "is_first_user": False,
        "granted_superadmin": False,
        "email_verified_on_create": True,
        "captcha_required": app_settings.captcha_required,
    }


@pytest.mark.asyncio
async def test_complete_default_org_name(session_factory):
    await _seed(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        assert _complete(client, create_signup_token("n@example.com")).status_code == 200
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.email == "n@example.com"))
        org = await db.get(Organization, user.org_id)
    assert org.name.startswith("newbie's Organization ")


@pytest.mark.asyncio
async def test_complete_token_single_use(session_factory):
    """A link redeems once. Its jti is spent by the request that creates the
    account; a token whose jti is already spent answers 400 and stores nothing."""
    await _seed(session_factory)
    token = create_signup_token("new@example.com")
    with TestClient(_make_app(session_factory)) as client:
        assert _complete(client, token).status_code == 200
        replay = _complete(client, token, username="other")
        # The account now exists, so the replay meets the account check first.
        assert replay.status_code == 409 and _code(replay) == "account_exists"

        spent = create_signup_token("fresh@example.com")
        assert await state_db.claim_token(
            SIGNUP_JTI_SCOPE, decode_signup_token(spent).jti, 3600
        )
        res = _complete(client, spent, username="fresh")
    assert res.status_code == 400 and _code(res) == "invalid_signup_token"
    # Users only: pysqlite commits the org's savepoint at RELEASE, so on SQLite
    # the org of a rolled-back sign-up stays. The MySQL race test counts both.
    assert await _count(session_factory, User) == 2


@pytest.mark.asyncio
async def test_complete_rejects_bad_tokens(session_factory, monkeypatch):
    await _seed(session_factory)
    good = create_signup_token("new@example.com")
    payload = jwt.decode(good, options={"verify_signature": False})
    payload["exp"] = payload["iat"] - 1
    expired = jwt.encode(payload, app_settings.jwt_secret_key, algorithm=app_settings.jwt_algorithm)
    head, body, sig = good.split(".")
    tampered = f"{head}.{body}.{sig[:-2]}{'A' if sig[-2] != 'A' else 'B'}{sig[-1]}"
    wrong_type = create_email_verification_token(1, "new@example.com")
    with TestClient(_make_app(session_factory)) as client:
        for token in (expired, tampered, wrong_type, "garbage"):
            res = _complete(client, token)
            assert res.status_code == 400, (token, res.text)
            assert _code(res) == "invalid_signup_token"
        monkeypatch.setattr(app_settings, "jwt_secret_key", "y" * 64)
        res = _complete(client, good)
    assert res.status_code == 400 and _code(res) == "invalid_signup_token"
    assert await _count(session_factory, User) == 1


@pytest.mark.asyncio
async def test_complete_account_exists_when_email_registered_meanwhile(session_factory):
    await _seed(session_factory)
    token = create_signup_token("seed@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, token)
    assert res.status_code == 409 and _code(res) == "account_exists"
    assert await _count(session_factory, User) == 1


@pytest.mark.asyncio
async def test_complete_username_taken_case_insensitively(session_factory):
    """Review focus 4: a username that differs only in case is taken (MySQL's
    collation would refuse it at the INSERT), answered as a 409, never a 500."""
    await _seed(session_factory, username="Alice", email="alice@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, create_signup_token("new@example.com"), username="alice")
    assert res.status_code == 409, res.text
    assert _code(res) == "username_taken"
    assert isinstance(res.json()["detail"]["suggestion"], str)
    assert await _count(session_factory, User) == 1


@pytest.mark.asyncio
async def test_complete_username_integrity_error_maps_to_409(session_factory, monkeypatch):
    """The race: the pre-check passes, the INSERT then hits the unique index."""
    await _seed(session_factory, username="alice", email="alice@example.com")
    real = auth_module._signup_conflict
    calls = {"n": 0}

    async def _miss_first(db, email, username):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(db, email, username)

    monkeypatch.setattr(auth_module, "_signup_conflict", _miss_first)
    token = create_signup_token("new@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, token, username="alice")
        assert res.status_code == 409, res.text
        assert _code(res) == "username_taken"
        assert res.json()["detail"]["suggestion"] == "alice1"
        # The conflict did not burn the link.
        assert _complete(client, token, username="alice2").status_code == 200


@pytest.mark.asyncio
async def test_complete_org_name_taken(session_factory):
    await _seed(session_factory, username="seed")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, create_signup_token("new@example.com"), org_name="SEED org")
    assert res.status_code == 409 and _code(res) == "org_name_taken"
    assert await _count(session_factory, User) == 1
    assert await _count(session_factory, Organization) == 1


@pytest.mark.asyncio
async def test_complete_double_click_answers_account_exists_not_org_name(
    session_factory, monkeypatch
):
    """Two clicks race: the second passes the pre-check, then meets the first
    one's org name. The caller should be told to sign in, not to rename."""
    await _seed(session_factory, username="first", email="new@example.com")
    real = auth_module._signup_conflict
    calls = {"n": 0}

    async def _miss_first(db, email, username):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(db, email, username)

    monkeypatch.setattr(auth_module, "_signup_conflict", _miss_first)
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(
            client, create_signup_token("new@example.com"), username="second", org_name="first Org"
        )
    assert res.status_code == 409 and _code(res) == "account_exists", res.text
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_complete_every_default_org_name_taken_is_a_409(session_factory, monkeypatch):
    """No org name given and every default attempt collides: a retryable 409,
    never a 500, and the link is not spent."""
    await _seed(session_factory)
    monkeypatch.setattr(auth_module, "_default_org_name", lambda username: "seed Org")
    token = create_signup_token("new@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, token)
        assert res.status_code == 409, res.text
        assert res.json()["detail"] == "Registration conflict, please try again"
        monkeypatch.undo()
        assert _complete(client, token).status_code == 200
    assert await _count(session_factory, User) == 2


@pytest.mark.asyncio
async def test_complete_resubmit_after_username_conflict_succeeds(session_factory):
    """Review focus 2: a 409 does not burn the link."""
    await _seed(session_factory, username="taken", email="taken@example.com")
    token = create_signup_token("new@example.com")
    with TestClient(_make_app(session_factory)) as client:
        first = _complete(client, token, username="taken")
        assert first.status_code == 409 and _code(first) == "username_taken"
        second = _complete(client, token, username="mine", org_name="taken Org")
        assert second.status_code == 409 and _code(second) == "org_name_taken"
        third = _complete(client, token, username="mine")
    assert third.status_code == 200, third.text
    assert await _count(session_factory, User) == 2


@pytest.mark.asyncio
async def test_complete_setup_required_on_empty_install(session_factory):
    with TestClient(_make_app(session_factory)) as client:
        res = _complete(client, create_signup_token("first@example.com"))
    assert res.status_code == 403 and _code(res) == "setup_required"
    assert await _count(session_factory, User) == 0
