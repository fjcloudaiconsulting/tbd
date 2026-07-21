"""End-to-end audit wiring for the user-facing sensitive operations.

PR2 of the notification train: five sensitive routes that wrote
business state but no `audit_events` row. This file pins the four
user-facing ones; the fifth (admin plan change) lives in
`test_audit_wiring.py` next to the other admin-org audit pins.

Each route is exercised via a real `TestClient`, the business state
mutation is confirmed (so we don't silently break the route), and the
audit row is asserted with the expected `event_type`, `outcome` and
actor identity.

The audit write uses the independent-session pattern (opens its own
session through the engine-wide factory). Tests override
`get_session_factory` onto the same in-memory SQLite factory the
business session uses so the row is visible to the test.

NO `dispatch_notification` is called from any of these routes — that
wiring is PR3 of the notification train. These tests are guardrails
against PR3 (or any later refactor) silently dropping the trigger
source that the notification dispatcher will subscribe to.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pyotp
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import Depends as _Depends
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.models.notification import Notification, NotificationCategory
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.security import hash_password
from app.services.mfa_service import encrypt_secret, generate_totp_secret


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
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    """SlowAPI ``Limiter`` is a module-level singleton; without a reset
    a 5/hour counter from one test bleeds into the next."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _mfa_key(monkeypatch):
    """Pin a Fernet key for the MFA encryption helper. Empty by default
    in test settings (matches dev .env), but every mfa_service call
    raises MfaConfigError without one. Used by both the test setup
    (encrypt the seeded TOTP secret) and the route under test (decrypt
    it during /mfa/enable verification)."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(app_settings, "mfa_encryption_key", key)


def _make_app(session_factory, user_id: int, *, router):
    """Wire a FastAPI app with all three audit-relevant overrides:

    - ``get_db`` → in-memory SQLite session
    - ``get_current_user`` → user loaded from THE SAME session (so
      handler mutations persist on commit; bug magnet)
    - ``get_session_factory`` → the same factory, so the
      independent-session audit write lands in the test DB
    """
    app = FastAPI()
    # SlowAPI handler chain — both /users/me and /users/me/password are
    # @limiter.limit decorated.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    # Depends on the same `get_db` so FastAPI's per-request dependency
    # cache hands the same session to override AND to the route.
    async def override_current_user(
        request: Request,
        db: AsyncSession = _Depends(get_db),
    ) -> User:
        request.state.auth_method = "jwt"  # interactive-session guard (spec §7)
        user = await db.get(User, user_id)
        assert user is not None
        await db.refresh(user, ["organization"])
        return user

    def override_session_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(router)
    return app


async def _seed_user(
    session_factory,
    *,
    email: str = "alice@acme.io",
    password: str = "starting-password-1",
    mfa_enabled: bool = False,
    totp_secret: str | None = None,
) -> dict:
    async with session_factory() as db:
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.commit()
        user = User(
            org_id=org.id,
            username="alice",
            email=email,
            password_hash=hash_password(password),
            role=Role.OWNER,
            is_active=True,
            email_verified=True,
            password_set=True,
            mfa_enabled=mfa_enabled,
            totp_secret=totp_secret,
        )
        db.add(user)
        await db.commit()
        return {"user_id": user.id, "org_id": org.id, "email": email}


# ── password change ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_change_writes_audit(session_factory):
    """POST /users/me/password (success) writes a
    ``user.password.changed`` audit row that PR3 will use as the
    trigger for the security notification."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": "starting-password-1",
                "new_password": "brand-new-passw0rd",
            },
        )
    assert res.status_code == 204, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.password.changed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["user_id"]
    assert row.actor_email == seed["email"]
    assert row.target_org_id == seed["org_id"]
    # Target is self — there is no target_user_id column on
    # audit_events. The actor identity carries the user-target signal,
    # and PR3's dispatcher keys off (actor_user_id, event_type).
    assert row.detail is not None
    # Rotation (not initial set) — password_set was True.
    assert row.detail.get("password_set_initial") is False

    # PR3: a security notification row was dispatched to the actor,
    # carrying the audit_event_id for forensic correlation.
    async with session_factory() as db:
        notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "user.password.changed"
                )
            )
        ).scalars().all()
    assert len(notifs) == 1
    notif = notifs[0]
    assert notif.user_id == seed["user_id"]
    assert notif.category == NotificationCategory.SECURITY
    assert notif.title == "Your password was changed"
    assert notif.audit_event_id == row.id
    assert notif.link_url == "/settings/security"


@pytest.mark.asyncio
async def test_password_change_failure_writes_no_audit(session_factory):
    """Wrong current_password is rejected with 400 BEFORE the audit
    write — failure-path auditing is intentionally not added in this
    PR (separate scope per the audit-gap-closures task). Pin that no
    spurious row is emitted so this gap closure doesn't sneak failure
    auditing in.
    """
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": "wrong-password",
                "new_password": "brand-new-passw0rd",
            },
        )
    assert res.status_code == 400, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.password.changed"
                )
            )
        ).scalars().all()
    assert len(rows) == 0


# ── email change ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_writes_audit_with_old_email(session_factory):
    """PUT /users/me email change captures the OLD email in actor_email
    (the user's identity at event time) and the NEW email in
    detail.new_email. Self-target — no target_user_id column."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={
                "email": "new-address@acme.io",
                "current_password": "starting-password-1",
            },
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.email.changed"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["user_id"]
    # actor_email carries the OLD identity — the value the audit row
    # attests to at the time of the event. After the swap, the user's
    # current email is the new one, but the audit row must point at
    # who lost the address.
    assert row.actor_email == "alice@acme.io"
    assert row.target_org_id == seed["org_id"]
    assert row.detail is not None
    assert row.detail["old_email"] == "alice@acme.io"
    assert row.detail["new_email"] == "new-address@acme.io"

    # PR3: security notification dispatched to the actor. The body
    # carries the NEW email (so a recipient receiving this at the OLD
    # address can confirm where the account was moved to).
    async with session_factory() as db:
        notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "user.email.changed"
                )
            )
        ).scalars().all()
    assert len(notifs) == 1
    notif = notifs[0]
    assert notif.user_id == seed["user_id"]
    assert notif.category == NotificationCategory.SECURITY
    assert notif.title == "Your account email was changed"
    assert "new-address@acme.io" in notif.body
    assert notif.audit_event_id == row.id


@pytest.mark.asyncio
async def test_email_change_no_change_writes_no_audit(session_factory):
    """PUT /users/me without an email field (or with the same email)
    must NOT emit ``user.email.changed`` — otherwise an unrelated
    profile edit would spam the security notification channel."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"first_name": "Alicia"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.email.changed"
                )
            )
        ).scalars().all()
    assert len(rows) == 0


# ── MFA enable + disable ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_enable_writes_audit(session_factory):
    """POST /auth/mfa/enable success writes ``user.mfa.enabled``.

    Setup: pre-stash an encrypted TOTP secret on the user (the route
    expects it from a prior /mfa/setup call), then post a TOTP code
    that verifies against the secret.
    """
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=False,
    )
    code = pyotp.TOTP(secret).now()

    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/enable",
            json={"code": code},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.mfa.enabled"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["user_id"]
    assert row.actor_email == seed["email"]
    assert row.target_org_id == seed["org_id"]

    # PR3: security notification dispatched to the actor.
    async with session_factory() as db:
        notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "user.mfa.enabled"
                )
            )
        ).scalars().all()
    assert len(notifs) == 1
    notif = notifs[0]
    assert notif.user_id == seed["user_id"]
    assert notif.category == NotificationCategory.SECURITY
    assert notif.title == "Two-factor authentication enabled"
    assert notif.audit_event_id == row.id


@pytest.mark.asyncio
async def test_mfa_disable_writes_audit(session_factory):
    """POST /auth/mfa/disable success writes ``user.mfa.disabled`` —
    the security-critical signal (a real user can react if it wasn't
    them)."""
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": "starting-password-1"},
        )
    assert res.status_code == 200, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.mfa.disabled"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == AuditOutcome.SUCCESS
    assert row.actor_user_id == seed["user_id"]
    assert row.actor_email == seed["email"]
    assert row.target_org_id == seed["org_id"]

    # PR3: security notification dispatched to the actor. Body
    # encourages re-enable (architect-locked copy).
    async with session_factory() as db:
        notifs = (
            await db.execute(
                select(Notification).where(
                    Notification.event_type == "user.mfa.disabled"
                )
            )
        ).scalars().all()
    assert len(notifs) == 1
    notif = notifs[0]
    assert notif.user_id == seed["user_id"]
    assert notif.category == NotificationCategory.SECURITY
    assert notif.title == "Two-factor authentication disabled"
    assert "re-enabling" in notif.body
    assert notif.audit_event_id == row.id


@pytest.mark.asyncio
async def test_mfa_disable_wrong_password_writes_no_audit(session_factory):
    """Wrong password → 403, MFA stays enabled, no audit row written
    (failure-path auditing is out of scope for this PR)."""
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": "wrong-password"},
        )
    assert res.status_code == 403, res.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "user.mfa.disabled"
                )
            )
        ).scalars().all()
    assert len(rows) == 0

    # And MFA is still enabled — the business state didn't change.
    async with session_factory() as db:
        user = await db.get(User, seed["user_id"])
        assert user is not None
        assert user.mfa_enabled is True
