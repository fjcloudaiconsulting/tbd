"""Single-user SECURITY events email the account holder (dual-channel).

PR #517 made the org-admin/member fan-out paths dual-channel (in-app +
email) but deliberately left the four single-user security hooks
in-app-only. This file pins the email side of that follow-up:

- ``user.password.changed``  → email the account's current address.
- ``user.mfa.enabled``       → email the account address.
- ``user.mfa.disabled``      → email the account address (the louder
  signal of the two).
- ``user.email.changed``     → email BOTH addresses (operator decision):
  the OLD address gets a security alert naming the new address (the old
  inbox is what a hijack victim still controls), the NEW address gets a
  confirmation naming the old address.

Contracts pinned here:

- Security emails are force-on: a user who opted out of every email
  category (including a stale ``email_security=False`` row) still gets
  them.
- Best-effort: a raising mailer never fails the request, never rolls
  back the security action, and never rolls back the in-app row.
- The OLD address for the email-change alert is the pre-mutation value
  (snapshot-before-mutation pattern), not the post-change one.

The app factory here mirrors ``test_audit_wiring_user_ops.py`` rather
than ``tests/factories/app.py::make_test_app``: these routes MUTATE
``current_user`` and commit through the request session, so the
``get_current_user`` override must resolve the user from THE SAME
session ``get_db`` yields (FastAPI dependency cache). ``make_test_app``
resolvers open their own session, which would detach the user and
silently drop the mutation.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pyotp
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import Depends as _Depends
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

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.notification import (
    Notification,
    NotificationCategory,
    UserNotificationPreferences,
)
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.auth import router as auth_router
from app.routers.users import router as users_router
from app.security import (
    create_password_reset_token,
    hash_password,
    verify_password,
)
from app.services import notification_service
from app.services.mfa_service import encrypt_secret, generate_totp_secret


OLD_EMAIL = "alice@acme.io"
NEW_EMAIL = "new-address@acme.io"
PASSWORD = "starting-password-1"


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
    """SlowAPI ``Limiter`` is a module-level singleton; without a reset
    a 5/hour counter from one test bleeds into the next."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _mfa_key(monkeypatch):
    """Pin a Fernet key so mfa_service encrypt/decrypt works in tests."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(app_settings, "mfa_encryption_key", key)


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture every notification email the service layer tries to send.

    Patches ``send_notification_email`` in the ``notification_service``
    namespace (the name the shared best-effort helper calls), so both
    the pref gate and the failure-swallowing wrapper stay under test.
    """
    sent: list[dict] = []

    async def _fake_email(to, *, title, body, link_url=None):
        sent.append({"to": to, "title": title, "body": body, "link_url": link_url})
        return True

    monkeypatch.setattr(notification_service, "send_notification_email", _fake_email)
    return sent


@pytest.fixture
def failing_mailer(monkeypatch):
    """Make every notification email attempt blow up."""
    attempts: list[str] = []

    async def _boom(to, *, title, body, link_url=None):
        attempts.append(to)
        raise RuntimeError("mailgun is down")

    monkeypatch.setattr(notification_service, "send_notification_email", _boom)
    return attempts


def _make_app(session_factory, user_id: int, *, router):
    """Same-session override wiring (see module docstring)."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_current_user(
        db: AsyncSession = _Depends(get_db),
    ) -> User:
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
    email: str = OLD_EMAIL,
    password: str = PASSWORD,
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


async def _opt_out_of_everything(session_factory, user_id: int) -> None:
    """Write a preference row with every email channel OFF.

    ``email_security=False`` cannot be reached through the API (the PUT
    rejects it) but a stale row could exist; the force-on rule must win
    even then.
    """
    async with session_factory() as db:
        db.add(
            UserNotificationPreferences(
                user_id=user_id,
                email_security=False,
                email_account=False,
                email_org_admin=False,
                email_org_activity=False,
                in_app_security=True,
                in_app_account=False,
                in_app_org_admin=False,
                in_app_org_activity=False,
            )
        )
        await db.commit()


async def _notif_rows(session_factory, event_type: str) -> list[Notification]:
    async with session_factory() as db:
        return (
            (
                await db.execute(
                    select(Notification).where(
                        Notification.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


# ── password change ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_change_sends_security_email(session_factory, sent_emails):
    """Password change writes the in-app row AND emails the account's
    current address with the same security copy."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": PASSWORD,
                "new_password": "brand-new-passw0rd",
            },
        )
    assert res.status_code == 204, res.text

    notifs = await _notif_rows(session_factory, "user.password.changed")
    assert len(notifs) == 1
    assert notifs[0].category == NotificationCategory.SECURITY

    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == OLD_EMAIL
    assert email["title"] == "Your password was changed"
    assert email["link_url"] == "/settings/security"


@pytest.mark.asyncio
async def test_password_change_email_force_on_despite_optout(
    session_factory, sent_emails
):
    """A user opted out of EVERY email category (even a stale
    email_security=False row) still receives the security email."""
    seed = await _seed_user(session_factory)
    await _opt_out_of_everything(session_factory, seed["user_id"])
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": PASSWORD,
                "new_password": "brand-new-passw0rd",
            },
        )
    assert res.status_code == 204, res.text

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == OLD_EMAIL


@pytest.mark.asyncio
async def test_password_change_survives_mailer_failure(
    session_factory, failing_mailer
):
    """A raising mailer must not fail the request, roll back the
    password change, or roll back the in-app row."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": PASSWORD,
                "new_password": "brand-new-passw0rd",
            },
        )
    assert res.status_code == 204, res.text
    assert failing_mailer == [OLD_EMAIL]  # the send was attempted

    # The security action stuck.
    async with session_factory() as db:
        user = await db.get(User, seed["user_id"])
        assert verify_password("brand-new-passw0rd", user.password_hash)

    # The in-app row stuck.
    notifs = await _notif_rows(session_factory, "user.password.changed")
    assert len(notifs) == 1


# ── MFA enable / disable ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_enable_sends_security_email(session_factory, sent_emails):
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=False,
    )
    code = pyotp.TOTP(secret).now()
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post("/api/v1/auth/mfa/enable", json={"code": code})
    assert res.status_code == 200, res.text

    notifs = await _notif_rows(session_factory, "user.mfa.enabled")
    assert len(notifs) == 1

    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == OLD_EMAIL
    assert email["title"] == "Two-factor authentication enabled"


@pytest.mark.asyncio
async def test_mfa_disable_sends_security_email(session_factory, sent_emails):
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable", json={"password": PASSWORD}
        )
    assert res.status_code == 200, res.text

    notifs = await _notif_rows(session_factory, "user.mfa.disabled")
    assert len(notifs) == 1

    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == OLD_EMAIL
    assert email["title"] == "Two-factor authentication disabled"


@pytest.mark.asyncio
async def test_mfa_disable_survives_mailer_failure(session_factory, failing_mailer):
    """MFA disable is the security-sensitive hook: a broken mailer must
    not fail the request or resurrect MFA."""
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/disable", json={"password": PASSWORD}
        )
    assert res.status_code == 200, res.text
    assert failing_mailer == [OLD_EMAIL]

    async with session_factory() as db:
        user = await db.get(User, seed["user_id"])
        assert user.mfa_enabled is False

    notifs = await _notif_rows(session_factory, "user.mfa.disabled")
    assert len(notifs) == 1


# ── email change: BOTH addresses ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_change_emails_both_addresses(session_factory, sent_emails):
    """The OLD address gets the security alert (naming the new address),
    the NEW address gets the confirmation (naming the old address). The
    old-address value is the pre-mutation snapshot."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": NEW_EMAIL, "current_password": PASSWORD},
        )
    assert res.status_code == 200, res.text

    # In-app row still written exactly once (to the account, whose
    # address is now the new one).
    notifs = await _notif_rows(session_factory, "user.email.changed")
    assert len(notifs) == 1

    assert len(sent_emails) == 2
    # Old-address alert goes out FIRST — it is the critical one.
    alert = sent_emails[0]
    assert alert["to"] == OLD_EMAIL
    assert alert["title"] == "Your account email was changed"
    assert NEW_EMAIL in alert["body"]
    assert "wasn't you" in alert["body"]

    confirmation = sent_emails[1]
    assert confirmation["to"] == NEW_EMAIL
    assert confirmation["title"] == "This address is now your login email"
    assert OLD_EMAIL in confirmation["body"]


@pytest.mark.asyncio
async def test_email_change_alert_force_on_despite_optout(
    session_factory, sent_emails
):
    seed = await _seed_user(session_factory)
    await _opt_out_of_everything(session_factory, seed["user_id"])
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": NEW_EMAIL, "current_password": PASSWORD},
        )
    assert res.status_code == 200, res.text
    assert [e["to"] for e in sent_emails] == [OLD_EMAIL, NEW_EMAIL]


@pytest.mark.asyncio
async def test_email_change_survives_mailer_failure(session_factory, failing_mailer):
    """Both sends raising must not fail the request or roll back the
    email change / in-app row. Both sends are still ATTEMPTED (the first
    failure does not suppress the second address)."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put(
            "/api/v1/users/me",
            json={"email": NEW_EMAIL, "current_password": PASSWORD},
        )
    assert res.status_code == 200, res.text
    assert failing_mailer == [OLD_EMAIL, NEW_EMAIL]

    async with session_factory() as db:
        user = await db.get(User, seed["user_id"])
        assert user.email == NEW_EMAIL

    notifs = await _notif_rows(session_factory, "user.email.changed")
    assert len(notifs) == 1


@pytest.mark.asyncio
async def test_profile_edit_without_email_change_sends_nothing(
    session_factory, sent_emails
):
    """An unrelated profile edit must not trigger any security email."""
    seed = await _seed_user(session_factory)
    app = _make_app(session_factory, seed["user_id"], router=users_router)
    with TestClient(app) as client:
        res = client.put("/api/v1/users/me", json={"first_name": "Alicia"})
    assert res.status_code == 200, res.text
    assert sent_emails == []


# ── password RESET (forgot-password completion, unauthenticated) ─────────────


@pytest.mark.asyncio
async def test_password_reset_sends_security_email(session_factory, sent_emails):
    """A completed password reset writes the in-app row (FK-correlated to
    the audit event) AND emails the account address. This is the
    account-takeover path — the highest-value alert of the batch."""
    seed = await _seed_user(session_factory)
    token = create_password_reset_token(seed["user_id"])
    # reset-password is unauthenticated; the router override for
    # get_current_user is unused by this route.
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "brand-new-passw0rd"},
        )
    assert res.status_code == 200, res.text

    notifs = await _notif_rows(session_factory, "user.password.reset")
    assert len(notifs) == 1
    assert notifs[0].category == NotificationCategory.SECURITY
    # FK-correlated to the audit row that triggered it.
    assert notifs[0].audit_event_id is not None

    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == OLD_EMAIL
    assert email["title"] == "Your password was reset"
    assert email["link_url"] == "/settings/security"


@pytest.mark.asyncio
async def test_password_reset_email_force_on_despite_optout(
    session_factory, sent_emails
):
    """A user opted out of every email category still receives the
    reset alert (security is force-on)."""
    seed = await _seed_user(session_factory)
    await _opt_out_of_everything(session_factory, seed["user_id"])
    token = create_password_reset_token(seed["user_id"])
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "brand-new-passw0rd"},
        )
    assert res.status_code == 200, res.text
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == OLD_EMAIL


@pytest.mark.asyncio
async def test_password_reset_survives_mailer_failure(
    session_factory, failing_mailer
):
    """A raising mailer must not fail the reset, revert the password, or
    roll back the in-app row."""
    seed = await _seed_user(session_factory)
    token = create_password_reset_token(seed["user_id"])
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "brand-new-passw0rd"},
        )
    assert res.status_code == 200, res.text
    assert failing_mailer == [OLD_EMAIL]  # the send was attempted

    async with session_factory() as db:
        user = await db.get(User, seed["user_id"])
        assert verify_password("brand-new-passw0rd", user.password_hash)

    notifs = await _notif_rows(session_factory, "user.password.reset")
    assert len(notifs) == 1


# ── MFA recovery-code regeneration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mfa_regenerate_sends_security_email(session_factory, sent_emails):
    """Regenerating recovery codes writes the in-app row (FK-correlated)
    AND emails the account address."""
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery-codes", json={"password": PASSWORD}
        )
    assert res.status_code == 200, res.text

    notifs = await _notif_rows(
        session_factory, "user.mfa.recovery_codes.regenerated"
    )
    assert len(notifs) == 1
    assert notifs[0].category == NotificationCategory.SECURITY
    assert notifs[0].audit_event_id is not None

    assert len(sent_emails) == 1
    email = sent_emails[0]
    assert email["to"] == OLD_EMAIL
    assert email["title"] == "Recovery codes regenerated"


@pytest.mark.asyncio
async def test_mfa_regenerate_survives_mailer_failure(
    session_factory, failing_mailer
):
    """A raising mailer must not fail the regeneration or roll back the
    in-app row."""
    secret = generate_totp_secret()
    seed = await _seed_user(
        session_factory,
        totp_secret=encrypt_secret(secret),
        mfa_enabled=True,
    )
    app = _make_app(session_factory, seed["user_id"], router=auth_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/mfa/recovery-codes", json={"password": PASSWORD}
        )
    assert res.status_code == 200, res.text
    assert failing_mailer == [OLD_EMAIL]

    notifs = await _notif_rows(
        session_factory, "user.mfa.recovery_codes.regenerated"
    )
    assert len(notifs) == 1
