"""DB-01: new email addresses must be plain ASCII.

``users.email`` compares under an accent-insensitive collation, so an accented
local part or an internationalised domain can match a different ASCII address.
Every entry point that brings in a NEW address refuses a non-ASCII one with a
422 (a redirect code on the Google callback): password sign-up, Google
sign-up, invitation, self-service email change and the operator email change.
Lookups are untouched: an account already stored under a non-ASCII address
keeps signing in and can keep sending its own address.

Each entry point is tried with an accented local part, an IDN domain in
Unicode and an IDN domain in ``xn--`` form.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.captcha import REASON_OK, CaptchaVerifyResult
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.middleware.request_context import RequestContextMiddleware
from app.models import Base
from app.models.invitation import Invitation
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.admin_users import router as admin_users_router
from app.routers.auth import router as auth_router
from app.routers.org_members import router as org_members_router
from app.routers.users import router as users_router
from app.schemas.admin_users import AdminEmailChangeRequest
from app.schemas.auth import RegisterRequest
from app.schemas.email_address import (
    NON_ASCII_EMAIL_DETAIL,
    google_sign_in_allowed,
    is_ascii_email,
)
from app.schemas.invitation import InvitationCreateRequest
from app.security import hash_password

PASSWORD = "correct-horse-battery-1"

ACCENTED_LOCAL = "josé@example.com"
IDN_UNICODE = "jose@münchen.de"
IDN_PUNYCODE = "jose@xn--mnchen-3ya.de"
NON_ASCII = [ACCENTED_LOCAL, IDN_UNICODE, IDN_PUNYCODE]
# An existing account stored under a non-ASCII address (from before DB-01).
LEGACY_EMAIL = "renée@example.com"


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII + ["a@b.xn--p1ai", "a@XN--MNCHEN-3YA.de"])
def test_is_ascii_email_refuses_accents_and_idn(address):
    assert is_ascii_email(address) is False


@pytest.mark.parametrize("address", ["jose@example.com", "j.o-s_e+tag@sub.example.co.uk"])
def test_is_ascii_email_accepts_plain_ascii(address):
    assert is_ascii_email(address) is True


@pytest.mark.parametrize(
    ("google", "stored", "allowed"),
    [
        ("bob@example.com", None, True),  # ordinary sign-up
        ("bob@example.com", "bob@example.com", True),  # ordinary sign-in
        (ACCENTED_LOCAL, None, False),  # no new non-ASCII account
        (IDN_UNICODE, None, False),
        (LEGACY_EMAIL, LEGACY_EMAIL, True),  # existing account, its own address
        ("renÉe@example.com", LEGACY_EMAIL, True),  # case only
        ("renée@example.com", LEGACY_EMAIL, True),  # NFD spelling of the same
        ("renee@example.com", LEGACY_EMAIL, False),  # look-alike matched by collation
        ("bób@example.com", "bob@example.com", False),  # the other direction
    ],
)
def test_google_sign_in_allowed(google, stored, allowed):
    """Wrong implementations: trusting whatever the accent-insensitive lookup
    returned, or refusing every non-ASCII address (locks existing accounts
    out)."""
    assert google_sign_in_allowed(google, stored) is allowed


# ── schemas ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII)
def test_register_schema_refuses_non_ascii(address):
    with pytest.raises(ValidationError, match="only ASCII"):
        RegisterRequest(username="jose", email=address, password=PASSWORD)


@pytest.mark.parametrize("address", NON_ASCII)
def test_invitation_schema_refuses_non_ascii(address):
    with pytest.raises(ValidationError, match="only ASCII"):
        InvitationCreateRequest(email=address, role="member")


@pytest.mark.parametrize("address", NON_ASCII)
@pytest.mark.parametrize("field", ["new_email", "new_email_confirm"])
def test_admin_email_change_schema_refuses_non_ascii(address, field):
    body = {"new_email": "ok@example.com", "new_email_confirm": "ok@example.com",
            "reason": "ticket 123", field: address}
    with pytest.raises(ValidationError, match="only ASCII"):
        AdminEmailChangeRequest(**body)


def test_schemas_still_accept_ascii():
    assert RegisterRequest(username="jose", email="Jose@Example.com", password=PASSWORD)
    assert InvitationCreateRequest(email="jose@example.com", role="member")
    assert AdminEmailChangeRequest(new_email="a@example.com", new_email_confirm="a@example.com",
                                   reason="ticket 123")


# ── harness ──────────────────────────────────────────────────────────────────


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
        org = Organization(name="Acme", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        db.add_all([
            User(org_id=org.id, username="owner", email="owner@example.com",
                 password_hash=hash_password(PASSWORD), role=Role.OWNER,
                 is_superadmin=True, is_active=True, email_verified=True),
            User(org_id=org.id, username="legacy", email=LEGACY_EMAIL,
                 password_hash=hash_password(PASSWORD), role=Role.MEMBER,
                 is_active=True, email_verified=True),
        ])
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

    async def _no_mail(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(auth_module, "verify_captcha", _ok)
    monkeypatch.setattr(auth_module, "send_verification_email", _no_mail)
    monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(app_settings, "app_url", "http://localhost")


def _make_app(session_factory, *, as_user: str | None = None) -> FastAPI:
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
    if as_user is not None:
        async def override_current_user(
            request: Request, db: AsyncSession = Depends(get_db)
        ) -> User:
            request.state.auth_method = "jwt"
            user = await db.scalar(select(User).where(User.username == as_user))
            await db.refresh(user, ["organization"])
            return user

        app.dependency_overrides[get_current_user] = override_current_user
    for router in (auth_router, org_members_router, users_router, admin_users_router):
        app.include_router(router)
    return app


async def _user_count(factory) -> int:
    async with factory() as db:
        return int(await db.scalar(select(func.count()).select_from(User)))


def _assert_ascii_422(res) -> None:
    assert res.status_code == 422, res.text
    assert "only ASCII" in res.text


# ── password sign-up ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII)
async def test_register_refuses_non_ascii(session_factory, address):
    before = await _user_count(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = client.post("/api/v1/auth/register", json={
            "username": "jose", "email": address, "password": PASSWORD,
            "captcha_token": "ok",
        })
    _assert_ascii_422(res)
    assert await _user_count(session_factory) == before


async def test_register_accepts_ascii(session_factory):
    with TestClient(_make_app(session_factory)) as client:
        res = client.post("/api/v1/auth/register", json={
            "username": "jose", "email": "jose@example.com", "password": PASSWORD,
            "captcha_token": "ok",
        })
    assert res.status_code == 201, res.text


async def test_existing_non_ascii_account_still_signs_in(session_factory):
    """Lookups are unchanged: the legacy account logs in with its address."""
    with TestClient(_make_app(session_factory)) as client:
        res = client.post("/api/v1/auth/login",
                          json={"login": LEGACY_EMAIL, "password": PASSWORD})
    assert res.status_code == 200, res.text
    assert res.json().get("access_token")


# ── invitation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII)
async def test_invitation_refuses_non_ascii(session_factory, address):
    with TestClient(_make_app(session_factory, as_user="owner")) as client:
        res = client.post("/api/v1/orgs/invitations",
                          json={"email": address, "role": "member"})
    _assert_ascii_422(res)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Invitation)) == 0


# ── self-service email change ────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII)
async def test_profile_email_change_refuses_non_ascii(session_factory, address):
    with TestClient(_make_app(session_factory, as_user="owner")) as client:
        res = client.put("/api/v1/users/me",
                         json={"email": address, "current_password": PASSWORD})
    assert res.status_code == 422, res.text
    assert res.json()["detail"] == NON_ASCII_EMAIL_DETAIL
    async with session_factory() as db:
        owner = await db.scalar(select(User).where(User.username == "owner"))
        assert owner.email == "owner@example.com" and owner.pending_email is None


async def test_profile_update_keeps_working_for_a_non_ascii_account(session_factory):
    """Only a CHANGE is checked: the legacy account sending its own address
    with another edit is not refused."""
    with TestClient(_make_app(session_factory, as_user="legacy")) as client:
        res = client.put("/api/v1/users/me",
                         json={"email": LEGACY_EMAIL.upper(), "first_name": "Renee"})
    assert res.status_code == 200, res.text
    async with session_factory() as db:
        legacy = await db.scalar(select(User).where(User.username == "legacy"))
        assert legacy.first_name == "Renee" and legacy.email == LEGACY_EMAIL


# ── operator email change ────────────────────────────────────────────────────


@pytest.mark.parametrize("address", NON_ASCII)
async def test_admin_email_change_refuses_non_ascii(session_factory, address):
    async with session_factory() as db:
        target = await db.scalar(select(User.id).where(User.username == "legacy"))
    with TestClient(_make_app(session_factory, as_user="owner")) as client:
        res = client.post(f"/api/v1/admin/users/{target}/email-change", json={
            "new_email": address, "new_email_confirm": address, "reason": "ticket 123",
        })
    _assert_ascii_422(res)
    async with session_factory() as db:
        legacy = await db.scalar(select(User).where(User.username == "legacy"))
        assert legacy.pending_email is None


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
                                       "given_name": "Jose", "family_name": "Ruiz"})

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)


def _google_callback(client: TestClient):
    client.cookies.set("oauth_state", "matching-state")
    return client.get("/api/v1/auth/google/callback",
                      params={"code": "dummy", "state": "matching-state"},
                      follow_redirects=False)


@pytest.mark.parametrize("address", NON_ASCII)
async def test_google_signup_refuses_non_ascii(session_factory, monkeypatch, address):
    _patch_google(monkeypatch, address)
    before = await _user_count(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _google_callback(client)
    assert res.status_code == 307, res.text
    assert res.headers["location"] == "http://localhost/login?sso_error=email_unsupported"
    assert await _user_count(session_factory) == before


async def test_google_signup_accepts_ascii(session_factory, monkeypatch):
    _patch_google(monkeypatch, "jose@example.com")
    with TestClient(_make_app(session_factory)) as client:
        res = _google_callback(client)
    assert res.status_code == 302, res.text
    assert "#token=" in res.headers["location"]


async def test_google_sign_in_of_an_existing_non_ascii_account_still_works(
    session_factory, monkeypatch
):
    _patch_google(monkeypatch, LEGACY_EMAIL)
    before = await _user_count(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _google_callback(client)
    assert res.status_code == 302, res.text
    assert "#token=" in res.headers["location"]
    assert await _user_count(session_factory) == before
