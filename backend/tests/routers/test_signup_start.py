"""TBD-654: ``POST /api/v1/auth/signup/start``, step 1 of email-first sign-up.

The answer must not tell account states apart: the same status, body and
headers for a new, existing, legacy unverified, inactive or capped address,
every mail queued in the background and none awaited inline, and nothing
stored or reserved. Structure is asserted, never timing.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.captcha import REASON_OK, REASON_PROVIDER_REJECTED, CaptchaVerifyResult
from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_session_factory
from app.middleware.request_context import RequestContextMiddleware
from app.models import Base
from app.models.audit_event import AuditEvent
from app.models.subscription import Plan
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.routers import auth as auth_module
from app.routers.auth import router as auth_router
from app.security import hash_password
from app.services import signup_service

START = "/api/v1/auth/signup/start"
ACCEPTED = {"detail": "Check your email to continue signing up."}
SENDERS = (
    "send_signup_link_email",
    "send_existing_account_signup_email",
    "send_legacy_verification_email",
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
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def captcha_ok(monkeypatch):
    monkeypatch.setattr(app_settings, "captcha_required", True)
    calls: list[tuple] = []

    async def _ok(token, remote_ip):
        calls.append((token, remote_ip))
        return CaptchaVerifyResult(ok=True, reason=REASON_OK)

    monkeypatch.setattr(auth_module, "verify_captcha", _ok)
    return calls


@pytest.fixture
def senders(monkeypatch) -> dict[str, AsyncMock]:
    mocks = {name: AsyncMock(return_value=True) for name in SENDERS}
    for name, mock in mocks.items():
        monkeypatch.setattr(signup_service, name, mock)
    return mocks


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


async def _seed(factory) -> None:
    """One org with an active verified, a legacy unverified and an inactive user."""
    async with factory() as db:
        db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
        org = Organization(name="Seed Org", billing_cycle_day=1)
        db.add(org)
        await db.flush()
        for name, active, verified in (
            ("existing", True, True),
            ("legacy", True, False),
            ("inactive", False, True),
        ):
            db.add(User(
                org_id=org.id, username=name, email=f"{name}@example.com",
                password_hash=hash_password("seed-password-1"), role=Role.OWNER,
                is_active=active, email_verified=verified,
            ))
        await db.commit()


async def _audit(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as db:
        rows = await db.execute(select(AuditEvent).where(AuditEvent.event_type == event_type))
        return list(rows.scalars())


async def _count(factory, model) -> int:
    async with factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


def _start(client: TestClient, email: str):
    return client.post(START, json={"email": email, "captcha_token": "tok"})


def _shape(res) -> tuple:
    headers = {k: v for k, v in res.headers.items() if k not in ("x-request-id", "date")}
    return res.status_code, res.json(), sorted(headers.items())


# ── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_identical_response_for_every_path(session_factory, captcha_ok, senders):
    await _seed(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        shapes = [
            _shape(_start(client, email))
            for email in (
                "new@example.com",
                "existing@example.com",
                "legacy@example.com",
                "inactive@example.com",
                "new@example.com",  # capped: inside its 10-minute window
            )
        ]
    assert shapes[0][0] == 202
    assert shapes[0][1] == ACCEPTED
    assert all(s == shapes[0] for s in shapes), shapes
    rows = await _audit(session_factory, "auth.signup.started")
    assert [r.detail["path"] for r in rows] == [
        "new", "existing", "legacy_unverified", "inactive", "capped",
    ]


@pytest.mark.asyncio
async def test_start_queues_send_in_background_never_awaits(
    session_factory, captcha_ok, senders, monkeypatch
):
    await _seed(session_factory)
    queued: list = []
    awaited_at_add: list[int] = []
    real_add = auth_module.BackgroundTasks.add_task

    def _spy(self, func, *args, **kwargs):
        # Recorded while the handler runs, before any background task does.
        awaited_at_add.append(sum(m.await_count for m in senders.values()))
        queued.append(func)
        return real_add(self, func, *args, **kwargs)

    monkeypatch.setattr(auth_module.BackgroundTasks, "add_task", _spy)
    with TestClient(_make_app(session_factory)) as client:
        for email in ("new@example.com", "existing@example.com", "legacy@example.com"):
            assert _start(client, email).status_code == 202

    assert awaited_at_add == [0, 1, 2]  # nothing awaited inline, only the earlier tasks
    assert [q.func for q in queued] == [
        senders["send_signup_link_email"],
        senders["send_existing_account_signup_email"],
        senders["send_legacy_verification_email"],
    ]
    for mock in senders.values():
        mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_inactive_and_capped_send_nothing(session_factory, captcha_ok, senders):
    await _seed(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        _start(client, "inactive@example.com")
        _start(client, "existing@example.com")
        _start(client, "existing@example.com")  # capped
    assert senders["send_existing_account_signup_email"].await_count == 1
    assert senders["send_signup_link_email"].await_count == 0
    assert senders["send_legacy_verification_email"].await_count == 0


@pytest.mark.asyncio
async def test_start_stores_nothing(session_factory, captcha_ok, senders, monkeypatch):
    """No user, org, username or org name exists or is reserved after step 1."""
    await _seed(session_factory)
    users, orgs = await _count(session_factory, User), await _count(session_factory, Organization)
    with TestClient(_make_app(session_factory)) as client:
        assert _start(client, "fresh@example.com").status_code == 202
        assert await _count(session_factory, User) == users
        assert await _count(session_factory, Organization) == orgs
        assert client.get(
            "/api/v1/auth/check-username", params={"username": "fresh"}
        ).json() == {"available": True, "suggestion": None}
        login = client.post(
            "/api/v1/auth/login", json={"login": "fresh@example.com", "password": "whatever-1"}
        )
        assert login.status_code == 401
        monkeypatch.setattr(app_settings, "captcha_required", False)
        reg = client.post("/api/v1/auth/register", json={
            "username": "fresh", "email": "other@example.com",
            "password": "another-password-1", "org_name": "Fresh Org",
        })
        assert reg.status_code == 201, reg.text


@pytest.mark.asyncio
async def test_start_setup_required_on_empty_install(session_factory, captcha_ok, senders):
    with TestClient(_make_app(session_factory)) as client:
        res = _start(client, "first@example.com")
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "setup_required"
    assert captcha_ok == []
    assert all(m.await_count == 0 for m in senders.values())


@pytest.mark.asyncio
async def test_start_captcha_always_required(session_factory, senders, monkeypatch):
    """No first-user bypass and no token: the provider refusal is a 400."""
    await _seed(session_factory)
    monkeypatch.setattr(app_settings, "captcha_required", True)
    seen: list = []

    async def _rejected(token, remote_ip):
        seen.append(token)
        return CaptchaVerifyResult(ok=False, reason=REASON_PROVIDER_REJECTED)

    monkeypatch.setattr(auth_module, "verify_captcha", _rejected)
    with TestClient(_make_app(session_factory)) as client:
        res = client.post(START, json={"email": "new@example.com"})
    assert res.status_code == 400
    assert seen == [None]


@pytest.mark.asyncio
async def test_start_captcha_failure_audits_register_captcha_failed(
    session_factory, senders, monkeypatch
):
    await _seed(session_factory)
    monkeypatch.setattr(app_settings, "captcha_required", True)

    async def _rejected(token, remote_ip):
        return CaptchaVerifyResult(
            ok=False, reason=REASON_PROVIDER_REJECTED,
            provider_error_codes=("invalid-input-response",),
        )

    monkeypatch.setattr(auth_module, "verify_captcha", _rejected)
    with TestClient(_make_app(session_factory)) as client:
        res = _start(client, " New@Example.com ")
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "captcha_failed"
    rows = await _audit(session_factory, "auth.register.captcha_failed")
    assert len(rows) == 1
    assert rows[0].actor_email == "new@example.com"
    assert rows[0].detail["reason"] == REASON_PROVIDER_REJECTED
    assert await _audit(session_factory, "auth.signup.started") == []
    assert all(m.await_count == 0 for m in senders.values())


@pytest.mark.asyncio
async def test_start_audits_started_with_path(session_factory, captcha_ok, senders):
    await _seed(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        _start(client, "Legacy@Example.com")
    rows = await _audit(session_factory, "auth.signup.started")
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id is None and row.target_org_id is None
    assert row.actor_email == "legacy@example.com"
    assert row.outcome.value == "success"
    assert row.detail == {"path": "legacy_unverified"}


@pytest.mark.asyncio
async def test_start_refuses_non_ascii_email(session_factory, captcha_ok, senders):
    await _seed(session_factory)
    with TestClient(_make_app(session_factory)) as client:
        res = _start(client, "josé@example.com")
    assert res.status_code == 422
    assert await _audit(session_factory, "auth.signup.started") == []
