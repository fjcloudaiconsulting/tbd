"""Router tests for the superadmin PAT management API (Task 4, spec §8/§11).

Pins the security-critical invariants of the management surface:

- **Step-up matrix (spec §8):** password-set operators prove presence with
  ``current_password``; SSO operators with a fresh ``stepup_token`` that is
  *consumed* on success; MFA-enabled operators additionally need a fresh
  ``mfa_code``. Missing/wrong proof → 401.
- **Reveal-once + no-store (SEC-R5):** the plaintext appears only in the mint
  response body under ``Cache-Control: no-store`` and in NO audit row.
- **Server-side expiry cap (SEC-R7):** ``expires_in_days > max`` → 422.
- **Interactive-only (spec §7A):** a valid ``write`` PAT hitting any of the
  four routes → 403 (never a mint-successor path).
- **Audit taxonomy (spec §11):** created (success+failure) / revoked /
  revoked_all(count), detail carries name/scope/expiry/prefix — never secret.

These tests drive the REAL ``get_current_user`` seam (no override): a JWT
stamps ``auth_method="jwt"`` so ``require_interactive_session`` admits it, and
a ``pat_`` bearer routes through ``authenticate_pat`` (stamps ``"pat"``) so the
guard denies it. That is the only faithful way to exercise the §7A gate.
"""
from __future__ import annotations

import json
import secrets as _secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
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
from app.deps import get_session_factory
from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers.api_tokens import router as api_tokens_router
from app.security import create_access_token, hash_password
from app.services import notification_service
from app.services.api_token_service import hash_api_token


UTC = timezone.utc


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield f
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _mock_email(monkeypatch):
    """Stub the outbound notification email so no Mailgun call happens. The
    in-app ``Notification`` row still writes to the test DB."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        notification_service, "send_notification_email", AsyncMock(return_value=None)
    )


PASSWORD = "correct-horse-battery"


async def _seed_superadmin(
    factory,
    *,
    password_set: bool = True,
    mfa_enabled: bool = False,
    mfa_secret: str | None = None,
    stepup_token: str | None = None,
    stepup_expires_at: datetime | None = None,
    is_superadmin: bool = True,
) -> int:
    async with factory() as s:
        org = Organization(name="Platform", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        u = User(
            org_id=org.id,
            username="root",
            email="root@platform.io",
            first_name="Root",
            password_hash=hash_password(PASSWORD),
            role=Role.OWNER,
            is_superadmin=is_superadmin,
            is_active=True,
            email_verified=True,
            password_set=password_set,
            mfa_enabled=mfa_enabled,
            totp_secret=mfa_secret,
            stepup_token=stepup_token,
            stepup_token_expires_at=stepup_expires_at,
        )
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u.id


async def _mint_pat_row(factory, owner_id: int, *, scope: str = "write") -> str:
    plaintext = "pat_" + _secrets.token_urlsafe(32)
    async with factory() as s:
        owner = await s.get(User, owner_id)
        row = ApiToken(
            token_hash=hash_api_token(plaintext),
            token_prefix=plaintext[:14],
            name="leaked",
            scope=scope,
            created_by_user_id=owner.id,
            created_by_email=owner.email,
            expires_at=_naive_now() + timedelta(days=30),
        )
        s.add(row)
        await s.commit()
    return plaintext


def _make_client(factory) -> TestClient:
    """App with the REAL auth seam (get_current_user NOT overridden), so
    ``auth_method`` is stamped and ``require_interactive_session`` is real."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.include_router(api_tokens_router)
    return TestClient(app)


async def _jwt_for(factory, user_id: int) -> str:
    async with factory() as s:
        u = await s.get(User, user_id)
        return create_access_token(u.id, u.org_id, u.role.value)


def _jwt_h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _audit_rows(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as s:
        result = await s.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )
        return list(result.scalars().all())


BASE = "/api/v1/system/api-tokens"


# ── Step-up: password-set operator ──────────────────────────────────────────


async def test_mint_requires_password_for_password_user(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={"name": "cron", "scope": "write", "expires_in_days": 30},
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401


async def test_mint_rejects_wrong_password(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": "wrong",
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401


async def test_mint_returns_plaintext_once_and_no_store(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token"].startswith("pat_")
    assert body["scope"] == "write"
    assert body["prefix"] == body["token"][:14]
    assert r.headers["Cache-Control"] == "no-store"


async def test_mint_rejects_over_max_expiry(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "x",
                "scope": "read",
                "expires_in_days": 9999,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 422


# ── Step-up: SSO operator (stepup_token, consumed) ──────────────────────────


async def test_mint_sso_requires_stepup_token(factory):
    uid = await _seed_superadmin(factory, password_set=False)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={"name": "cron", "scope": "write", "expires_in_days": 30},
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401


async def test_mint_sso_consumes_stepup_token(factory):
    token = "sso-fresh-token-value"
    uid = await _seed_superadmin(
        factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=_naive_now() + timedelta(minutes=5),
    )
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "stepup_token": token,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 201, r.text
    # Consumed: the token is nulled so it can't be replayed (SEC F4).
    async with factory() as s:
        u = await s.get(User, uid)
        assert u.stepup_token is None
        assert u.stepup_token_expires_at is None


async def test_mint_sso_rejects_expired_stepup_token(factory):
    token = "sso-stale-token"
    uid = await _seed_superadmin(
        factory,
        password_set=False,
        stepup_token=token,
        stepup_expires_at=_naive_now() - timedelta(seconds=1),
    )
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "stepup_token": token,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401


# ── Step-up: MFA-enabled operator (additional TOTP) ─────────────────────────


@pytest.fixture
def _mfa_key(monkeypatch):
    """A valid Fernet key so ``encrypt_secret``/``decrypt_secret`` round-trip."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(app_settings, "mfa_encryption_key", key)
    return key


async def test_mint_mfa_requires_code(factory, _mfa_key):
    from app.services.mfa_service import encrypt_secret

    secret = pyotp.random_base32()
    uid = await _seed_superadmin(
        factory, password_set=True, mfa_enabled=True, mfa_secret=encrypt_secret(secret)
    )
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401  # missing mfa_code


async def test_mint_mfa_accepts_valid_code(factory, _mfa_key):
    from app.services.mfa_service import encrypt_secret

    secret = pyotp.random_base32()
    uid = await _seed_superadmin(
        factory, password_set=True, mfa_enabled=True, mfa_secret=encrypt_secret(secret)
    )
    jwt = await _jwt_for(factory, uid)
    code = pyotp.TOTP(secret).now()
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
                "mfa_code": code,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 201, r.text


async def test_mint_mfa_rejects_wrong_code(factory, _mfa_key):
    from app.services.mfa_service import encrypt_secret

    secret = pyotp.random_base32()
    uid = await _seed_superadmin(
        factory, password_set=True, mfa_enabled=True, mfa_secret=encrypt_secret(secret)
    )
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
                "mfa_code": "000000",
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401


async def test_mint_no_mfa_operator_not_asked(factory):
    """An operator WITHOUT MFA must not be forced to supply a code."""
    uid = await _seed_superadmin(factory, password_set=True, mfa_enabled=False)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 201, r.text


# ── Reveal-once / no-secret-in-audit (SEC-R5) ───────────────────────────────


async def test_audit_created_has_no_secret(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
    secret = r.json()["token"]
    rows = await _audit_rows(factory, "api_token.created")
    assert rows
    assert all(secret not in json.dumps(x.detail or {}) for x in rows)
    # And the success row carries the metadata the spec mandates.
    success = [x for x in rows if x.outcome == "success"]
    assert success and success[0].detail["prefix"] == secret[:14]


async def test_mint_failure_is_audited(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": "wrong",
            },
            headers=_jwt_h(jwt),
        )
    assert r.status_code == 401
    rows = await _audit_rows(factory, "api_token.created")
    assert rows and rows[0].outcome == "failure"
    assert rows[0].detail["reason"] == "step_up_failed"


# ── List ────────────────────────────────────────────────────────────────────


async def test_list_returns_metadata_no_secret(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        mint = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "read",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
        secret = mint.json()["token"]
        r = client.get(BASE, headers=_jwt_h(jwt))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["status"] == "active"
    assert item["scope"] == "read"
    assert "token" not in item
    assert secret not in json.dumps(body)


# ── Revoke ──────────────────────────────────────────────────────────────────


async def test_revoke_sets_revoked_at(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        mint = client.post(
            BASE,
            json={
                "name": "cron",
                "scope": "write",
                "expires_in_days": 30,
                "current_password": PASSWORD,
            },
            headers=_jwt_h(jwt),
        )
        tid = mint.json()["id"]
        r = client.delete(f"{BASE}/{tid}", headers=_jwt_h(jwt))
    assert r.status_code == 200
    async with factory() as s:
        row = await s.get(ApiToken, tid)
        assert row.revoked_at is not None
    rows = await _audit_rows(factory, "api_token.revoked")
    assert rows and rows[0].detail["api_token_id"] == tid


async def test_revoke_unknown_returns_404(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.delete(f"{BASE}/999999", headers=_jwt_h(jwt))
    assert r.status_code == 404


# ── Revoke-all (panic button) ───────────────────────────────────────────────


async def test_revoke_all_returns_count(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        for _ in range(3):
            client.post(
                BASE,
                json={
                    "name": "cron",
                    "scope": "write",
                    "expires_in_days": 30,
                    "current_password": PASSWORD,
                },
                headers=_jwt_h(jwt),
            )
        r = client.post(f"{BASE}/revoke-all", headers=_jwt_h(jwt))
    assert r.status_code == 200
    assert r.json()["revoked"] == 3
    rows = await _audit_rows(factory, "api_token.revoked_all")
    assert rows and rows[0].detail["count"] == 3


# ── Superadmin gate ─────────────────────────────────────────────────────────


async def test_non_superadmin_forbidden(factory):
    uid = await _seed_superadmin(factory, password_set=True, is_superadmin=False)
    jwt = await _jwt_for(factory, uid)
    with _make_client(factory) as client:
        r = client.get(BASE, headers=_jwt_h(jwt))
    assert r.status_code == 403


# ── Interactive-only: a valid write PAT is denied on all four routes (§7A) ──


async def test_write_pat_cannot_manage_tokens(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    pat = await _mint_pat_row(factory, uid, scope="write")
    h = _jwt_h(pat)  # a PAT bearer, deliberately
    with _make_client(factory) as client:
        assert client.get(BASE, headers=h).status_code == 403
        assert (
            client.post(
                BASE,
                json={"name": "x", "scope": "write", "expires_in_days": 30},
                headers=h,
            ).status_code
            == 403
        )
        assert client.delete(f"{BASE}/1", headers=h).status_code == 403
        assert client.post(f"{BASE}/revoke-all", headers=h).status_code == 403


# ── Rate limit on mint (§8, ARC-R2) ─────────────────────────────────────────


async def test_mint_is_rate_limited(factory):
    uid = await _seed_superadmin(factory, password_set=True)
    jwt = await _jwt_for(factory, uid)
    payload = {
        "name": "cron",
        "scope": "write",
        "expires_in_days": 30,
        "current_password": PASSWORD,
    }
    with _make_client(factory) as client:
        codes = [
            client.post(BASE, json=payload, headers=_jwt_h(jwt)).status_code
            for _ in range(11)
        ]
    # 10/hour: the first ten succeed, the eleventh is throttled.
    assert codes[:10] == [201] * 10
    assert codes[10] == 429
