"""TBD-654: ``POST /register`` answers the same way whether or not the email
already has an account.

A new email creates the account and mails a verification link. An email that
already has an account creates and changes nothing, and mails the owner a
"someone tried to sign up with your email" notice, at most once per window.
Both answer the same status and body, and both pay for the password hash.
Refusals that depend only on fields the caller chose (username, org name) are
decided before the email is looked at, so they answer alike for both.
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
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password

OWNER_EMAIL = "owner@example.com"
PASSWORD = "another-password-1"


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
    async with factory() as db:
        db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
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
def sent(monkeypatch) -> dict[str, list[str]]:
    """Recipients of each mail ``/register`` can send, by kind."""
    out: dict[str, list[str]] = {"verification": [], "existing": []}

    async def _verification(to: str, token: str) -> bool:
        out["verification"].append(to)
        return True

    async def _existing(to: str) -> bool:
        out["existing"].append(to)
        return True

    monkeypatch.setattr(auth_module, "send_verification_email", _verification)
    monkeypatch.setattr(auth_module, "send_existing_account_signup_email", _existing)
    return out


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


async def _seed_owner(factory, *, is_active: bool = True) -> int:
    async with factory() as db:
        org = Organization(name="Owner Household", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id,
            username="owner",
            email=OWNER_EMAIL,
            password_hash=hash_password("owner-password-1"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=is_active,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _snapshot(factory) -> dict[str, Any]:
    """Everything a register could create or change."""
    async with factory() as db:
        users = (await db.execute(select(User).order_by(User.id))).scalars().all()
        orgs = await db.scalar(select(func.count()).select_from(Organization))
    return {
        "orgs": orgs,
        "users": [
            (u.id, u.username, u.email, u.password_hash, u.org_id, u.email_verified,
             u.first_name, u.last_name, u.is_active)
            for u in users
        ],
    }


def _register(client: TestClient, *, username: str, email: str, **extra: Any):
    return client.post("/api/v1/auth/register", json={
        "username": username, "email": email, "password": PASSWORD, **extra,
    })


async def test_new_and_existing_email_get_the_same_answer(session_factory, sent):
    await _seed_owner(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        new = _register(client, username="newcomer", email="newcomer@example.com")
        existing = _register(client, username="intruder", email=OWNER_EMAIL)

    assert new.status_code == existing.status_code == 201, (new.text, existing.text)
    assert new.json() == existing.json() == {"detail": auth_module.REGISTER_ACCEPTED_DETAIL}
    assert new.headers.get("content-type") == existing.headers.get("content-type")
    # Each path mailed only its own message.
    assert sent["verification"] == ["newcomer@example.com"]
    assert sent["existing"] == [OWNER_EMAIL]


async def test_existing_email_creates_and_changes_nothing(session_factory, sent):
    await _seed_owner(session_factory)
    before = await _snapshot(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _register(
            client, username="intruder", email="  Owner@Example.COM ",
            first_name="Mallory", org_name="Fresh Household",
        )
    assert res.status_code == 201, res.text
    assert await _snapshot(session_factory) == before
    assert sent["verification"] == []
    assert sent["existing"] == [OWNER_EMAIL]


async def test_existing_account_notice_is_rate_limited(session_factory, sent):
    """However many tries (from however many addresses), one notice per window."""
    await _seed_owner(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        answers = []
        for i in range(3):
            limiter.reset()  # as if each try came from a fresh address
            answers.append(_register(client, username=f"intruder{i}", email=OWNER_EMAIL))
    assert {(r.status_code, r.text) for r in answers} == {
        (201, answers[0].text)
    }
    assert sent["existing"] == [OWNER_EMAIL]


async def test_notice_window_is_per_account(session_factory, sent):
    """A throttled notice for one account does not silence another's."""
    await _seed_owner(session_factory)
    async with session_factory() as db:
        org = Organization(name="Other Household", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(User(
            org_id=org.id, username="other", email="other@example.com",
            password_hash=hash_password("x-password-1"), role=Role.OWNER,
            is_superadmin=False, is_active=True, email_verified=True,
        ))
        await db.commit()
    with TestClient(_make_app(session_factory)) as client:
        _register(client, username="intruder1", email=OWNER_EMAIL)
        _register(client, username="intruder2", email=OWNER_EMAIL)
        _register(client, username="intruder3", email="other@example.com")
    assert sent["existing"] == [OWNER_EMAIL, "other@example.com"]


async def test_inactive_account_gets_the_same_answer_and_no_mail(session_factory, sent):
    await _seed_owner(session_factory, is_active=False)
    before = await _snapshot(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, username="intruder", email=OWNER_EMAIL)
    assert res.status_code == 201, res.text
    assert res.json() == {"detail": auth_module.REGISTER_ACCEPTED_DETAIL}
    assert await _snapshot(session_factory) == before
    assert sent["existing"] == []


async def test_throttle_store_error_fails_closed_with_the_same_answer(
    session_factory, sent, monkeypatch
):
    """No notice when the throttle cannot be read, and no 500 that only an
    existing email could produce."""
    await _seed_owner(session_factory)

    async def _broken(scope: str, token: str, ttl_seconds: int) -> bool:
        raise OperationalError("claim", {}, Exception("down"))

    monkeypatch.setattr(auth_module.state_db, "claim_token", _broken)
    with TestClient(_make_app(session_factory)) as client:
        res = _register(client, username="intruder", email=OWNER_EMAIL)
    assert res.status_code == 201, res.text
    assert res.json() == {"detail": auth_module.REGISTER_ACCEPTED_DETAIL}
    assert sent["existing"] == []


async def test_both_paths_hash_the_password(session_factory, monkeypatch):
    """Wrong implementation: an early return for a known email that skips
    bcrypt, so the answer arrives measurably sooner."""
    await _seed_owner(session_factory)
    calls: list[str] = []
    real = auth_module.hash_password

    def _counting(password: str) -> str:
        calls.append(password)
        return real(password)

    monkeypatch.setattr(auth_module, "hash_password", _counting)
    with TestClient(_make_app(session_factory)) as client:
        _register(client, username="newcomer", email="newcomer@example.com")
        assert len(calls) == 1
        _register(client, username="intruder", email=OWNER_EMAIL)
        assert len(calls) == 2


async def test_taken_org_name_answers_409_for_new_and_existing_email(session_factory, sent):
    """Wrong implementation: the org-name check runs only while creating the
    account, so a taken name answers 409 for a new email and 201 for a
    registered one."""
    await _seed_owner(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        new = _register(client, username="newcomer", email="newcomer@example.com",
                        org_name="owner household")
        existing = _register(client, username="intruder", email=OWNER_EMAIL,
                             org_name="owner household")
    assert new.status_code == existing.status_code == 409
    assert new.json() == existing.json()
    assert sent == {"verification": [], "existing": []}


async def test_taken_username_answers_409_for_new_and_existing_email(session_factory, sent):
    await _seed_owner(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        new = _register(client, username="owner", email="newcomer@example.com")
        existing = _register(client, username="owner", email=OWNER_EMAIL)
    assert new.status_code == existing.status_code == 409
    assert new.json() == existing.json() == {"detail": "Username already taken"}
    assert sent == {"verification": [], "existing": []}
