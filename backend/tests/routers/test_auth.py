"""Auth router tests for L4.6 login audit emission.

Pins:
- ``POST /api/v1/auth/login`` writes a ``user.login.success`` audit row
  with ``detail.method == "password"`` for the actor.
- ``POST /api/v1/auth/mfa/recovery`` writes a ``user.login.success``
  audit row with ``detail.method == "mfa_recovery"`` once the second
  factor clears.

These tests are the only guard against a future refactor silently
dropping the audit-event emission that the L4.6 analytics
``logins_by_day`` series depends on.

Pattern:
- An in-memory SQLite session factory backs both the request session
  AND the independent audit-write session — ``get_session_factory``
  is overridden onto the same factory so the audit row lands in the
  DB the test queries.
- The audit write opens its OWN session (``record_audit_event``), so
  we must flush by closing that session and re-querying with the test
  factory. ``record_audit_event`` calls ``commit`` internally on the
  audit session, so the row is visible to a subsequent ``factory()``
  open without further work.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.security import (
    create_mfa_challenge_token,
    hash_password,
)
from app.services.mfa_service import (
    generate_recovery_codes,
    hash_recovery_code,
)


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
    """SlowAPI ``Limiter`` is a module-level singleton; without a reset
    the per-IP counter from one test bleeds into the next (a 429 on a
    perfectly good login)."""
    limiter.reset()
    yield
    limiter.reset()


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()
    # SlowAPI handler chain — without these the @limiter.limit decorator
    # raises an AttributeError on request.state.limiter.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_session_factory():
        # Returns the factory itself (FastAPI invokes the dependency to
        # produce the value; the value is the factory we hand out).
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(auth_router)
    return app


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    email: str = "alice@acme.io",
    username: str = "alice",
    password: str = "starting-password-1",
    mfa_enabled: bool = False,
    recovery_codes_plaintext: list[str] | None = None,
) -> dict:
    """Create org + user. Optionally set MFA state and pre-issued
    recovery codes (returned plaintext for the test to send back).
    """
    async with factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        recovery_field: str | None = None
        if recovery_codes_plaintext is not None:
            recovery_field = ",".join(
                hash_recovery_code(c) for c in recovery_codes_plaintext
            )
        user = User(
            org_id=org.id,
            username=username,
            email=email,
            password_hash=hash_password(password),
            role=Role.OWNER,
            is_superadmin=False,
            is_active=True,
            email_verified=True,
            mfa_enabled=mfa_enabled,
            recovery_codes=recovery_field,
        )
        db.add(user)
        await db.commit()
        return {"org_id": org.id, "user_id": user.id, "email": email}


async def _login_success_rows(factory) -> list[AuditEvent]:
    """Pull every ``user.login.success`` row from the test DB. The audit
    write commits on a session opened by ``record_audit_event``; opening
    a new session here is sufficient to observe it (no extra flush)."""
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "user.login.success"
            )
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_password_login_writes_user_login_success_audit(
    session_factory,
) -> None:
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": "starting-password-1"},
        )
    assert res.status_code == 200, res.json()
    assert "access_token" in res.json()

    rows = await _login_success_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "user.login.success"
    assert row.outcome.value == "success"
    assert row.actor_user_id == seed["user_id"]
    assert row.actor_email == seed["email"]
    # The discriminator is what the analytics service will eventually
    # split on. Pin the spelling.
    assert row.detail is not None
    assert row.detail.get("method") == "password"


@pytest.mark.asyncio
async def test_invalid_password_does_not_write_audit(session_factory) -> None:
    """Failed login must NOT leave a success row behind. (We don't
    currently emit a failure row either — this test pins the
    today-behavior so a future regression to "success on every POST"
    is caught.)"""
    await _seed_user(session_factory)
    app = _make_app(session_factory)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/login",
            json={"login": "alice", "password": "wrong-password"},
        )
    assert res.status_code == 401

    rows = await _login_success_rows(session_factory)
    assert rows == []


@pytest.mark.asyncio
async def test_mfa_recovery_writes_user_login_success_audit(
    session_factory,
) -> None:
    """``/auth/mfa/recovery`` is the second-factor completion path for
    users who lost their authenticator. Successfully consuming a
    recovery code is a login completion event and must emit the audit
    row with ``method=mfa_recovery``."""
    codes = generate_recovery_codes(count=3)
    seed = await _seed_user(
        session_factory,
        mfa_enabled=True,
        recovery_codes_plaintext=codes,
    )
    app = _make_app(session_factory)

    mfa_token = create_mfa_challenge_token(seed["user_id"])

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery",
            json={"mfa_token": mfa_token, "code": codes[0]},
        )
    assert res.status_code == 200, res.json()
    assert "access_token" in res.json()

    rows = await _login_success_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == seed["user_id"]
    assert row.detail is not None
    assert row.detail.get("method") == "mfa_recovery"
