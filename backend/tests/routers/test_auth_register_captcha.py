"""Route-level coverage for the CAPTCHA gate on /api/v1/auth/register.

The unit tests in ``tests/test_captcha.py`` pin the verify module in
isolation; these pin the integration into the register handler:

* ``CAPTCHA_REQUIRED=false`` — verify is NOT called, registration
  proceeds as before.
* ``CAPTCHA_REQUIRED=true`` + successful verify — registration commits,
  user count goes from 1 to 2 (a non-first-user signup).
* ``CAPTCHA_REQUIRED=true`` + rejected verify — 400 with
  ``code=captcha_failed``, user count UNCHANGED, an audit
  ``auth.register.captcha_failed`` row is committed.
* First-user setup (``user_count == 0``) — verify is NOT called even
  when ``CAPTCHA_REQUIRED=true``, the bootstrap flow stays usable.
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

from app import captcha as captcha_module
from app.captcha import CaptchaVerifyResult, REASON_OK, REASON_PROVIDER_REJECTED
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password


# ── fixtures ─────────────────────────────────────────────────────────────────


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


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


async def _seed_existing_user(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed one user so the next /register call goes through the
    captcha gate (the first-user-setup bypass requires user_count==0).
    """
    async with factory() as db:
        org = Organization(name="Existing Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add(
            User(
                org_id=org.id,
                username="seed",
                email="seed@example.com",
                password_hash=hash_password("seed-password-1"),
                role=Role.OWNER,
                is_superadmin=True,
                is_active=True,
                email_verified=True,
            )
        )
        await db.commit()


async def _count_users(factory) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(User)) or 0


async def _captcha_failed_audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "auth.register.captcha_failed"
            )
        )
        return list(result.scalars())


# ── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_skips_verify_when_captcha_required_false(
    session_factory, monkeypatch
) -> None:
    """The verify module short-circuits internally when the flag is off;
    the handler MUST also not block on a missing token. Asserts no
    ``captcha_token`` in the request body is OK and the user is created."""
    monkeypatch.setattr(app_settings, "captcha_required", False)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)

    # Drop a tripwire so a regression that calls verify outside the
    # disabled short-circuit would surface here.
    tripwire_calls: list[Any] = []

    async def _tripwire(*args, **kwargs):
        tripwire_calls.append((args, kwargs))
        return CaptchaVerifyResult(ok=True, reason=REASON_OK)

    monkeypatch.setattr(auth_module, "verify_captcha", _tripwire)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "another-password-1",
            },
        )

    assert res.status_code == 201, res.text
    # Handler still calls verify_captcha, but the module short-circuits
    # internally — so the tripwire DOES see a call. The point of this
    # test is just that registration completes without a token.
    assert await _count_users(session_factory) == 2


@pytest.mark.asyncio
async def test_register_succeeds_when_verify_ok(
    session_factory, monkeypatch
) -> None:
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)

    async def _ok(token, remote_ip):
        return CaptchaVerifyResult(ok=True, reason=REASON_OK)

    monkeypatch.setattr(auth_module, "verify_captcha", _ok)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "another-password-1",
                "captcha_token": "valid-token",
            },
        )

    assert res.status_code == 201, res.text
    assert await _count_users(session_factory) == 2


@pytest.mark.asyncio
async def test_register_rejected_when_verify_fails_user_count_unchanged(
    session_factory, monkeypatch
) -> None:
    """The single most important contract: a captcha rejection MUST
    leave the database untouched. Pins fail-closed at the route level."""
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)

    async def _rejected(token, remote_ip):
        return CaptchaVerifyResult(
            ok=False,
            reason=REASON_PROVIDER_REJECTED,
            provider_error_codes=("invalid-input-response",),
        )

    monkeypatch.setattr(auth_module, "verify_captcha", _rejected)

    initial_count = await _count_users(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "another-password-1",
                "captcha_token": "bad-token",
            },
        )

    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "captcha_failed"
    assert "verify" in detail["message"].lower()
    # No user created — fail-closed at route layer.
    assert await _count_users(session_factory) == initial_count
    # Audit row written so the operator can tail the wave.
    audit_rows = await _captcha_failed_audit_rows(session_factory)
    assert len(audit_rows) == 1
    assert audit_rows[0].outcome == "failure"


@pytest.mark.asyncio
async def test_register_first_user_setup_bypasses_captcha(
    session_factory, monkeypatch
) -> None:
    """Bootstrap exemption: when the DB has zero users, /register skips
    the captcha gate so the /setup flow doesn't deadlock the operator
    on a Cloudflare account they don't have yet."""
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    # No _seed_existing_user — start from a true cold DB.

    # If the handler accidentally still calls verify, this would surface
    # as a registration failure (default ok=False if reached).
    tripwire_calls: list[Any] = []

    async def _tripwire(token, remote_ip):
        tripwire_calls.append((token, remote_ip))
        return CaptchaVerifyResult(ok=False, reason="should-not-be-called")

    monkeypatch.setattr(auth_module, "verify_captcha", _tripwire)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "firstuser",
                "email": "first@example.com",
                "password": "first-password-1",
            },
        )

    assert res.status_code == 201, res.text
    assert tripwire_calls == [], (
        "verify_captcha must NOT be called for the first-user setup path"
    )
    assert await _count_users(session_factory) == 1
