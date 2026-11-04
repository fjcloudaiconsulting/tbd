"""Route-level coverage for /api/v1/auth/register, first-user setup only.

TBD-654 moved every sign-up after the first to ``/signup/start`` and
``/signup/complete``. ``/register`` now:

* answers one fixed 403 ``setup_complete`` once any user exists, whatever the
  body says, so it reveals nothing about any email or username, and creates,
  sends and audits nothing;
* on an empty install skips the captcha (the operator has no widget yet) and
  creates the verified superadmin, audited as ``auth.register.success``;
* under ``APP_ENV=production`` demands ``SETUP_TOKEN`` for that first user,
  and refuses setup when the token is unset (INF-05).

The captcha gate and its ``auth.register.captcha_failed`` row live on
``/signup/start`` now; ``tests/routers/test_signup_start.py`` covers them, and
the request-id trust-boundary fence for the refusal row is kept below against
that route.
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

from app.captcha import CaptchaVerifyResult, REASON_PROVIDER_REJECTED
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
    # TBD-291: mounted here for the same reason `main.py` mounts it — it is what
    # sanitizes the inbound `X-Request-Id` and binds the safe value onto the log context
    # contextvars. Without it every `request_id` the handler records is None, and
    # the sanitizer fences below could not tell a sanitized value from a raw one.
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


async def _seed_default_plan(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as db:
        existing = await db.scalar(select(Plan).where(Plan.slug == "free"))
        if existing is None:
            db.add(Plan(slug="free", name="Free", is_active=True, sort_order=0))
            await db.commit()


async def _seed_existing_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    is_superadmin: bool = True,
) -> None:
    """Seed one user, which closes the first-user setup (user_count == 0).

    ``is_superadmin`` is still explicit, but TBD-365 changed WHY. There is
    now ONE first-ness predicate (``user_count == 0``), so this flag no
    longer changes any grant outcome — seeding any user closes the bootstrap
    regardless. It is kept because the *divergent* state (users present, zero
    superadmins) is the one the retired predicate mis-answered, and
    ``test_register_divergent_state_is_refused`` below builds it
    deliberately with ``is_superadmin=False``.

    ⚠ Do NOT reintroduce a second first-ness predicate to make this parameter
    load-bearing again. That is the escalation TBD-365 removed.
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
                is_superadmin=is_superadmin,
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

REGISTER = "/api/v1/auth/register"
SETUP_TOKEN = "operator-setup-token-for-tests-0123456789"


def _body(**overrides: Any) -> dict[str, Any]:
    return {
        "username": "founder",
        "email": "founder@example.com",
        "password": "founder-password-1",
        **overrides,
    }


def _captcha_tripwire(monkeypatch) -> list[Any]:
    calls: list[Any] = []

    async def _tripwire(token, remote_ip):
        calls.append((token, remote_ip))
        return CaptchaVerifyResult(ok=False, reason="should-not-be-called")

    monkeypatch.setattr(auth_module, "verify_captcha", _tripwire)
    return calls


async def _register_success_audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "auth.register.success"
            )
        )
        return list(result.scalars())


async def _all_audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        return list((await db.execute(select(AuditEvent))).scalars())


# ── after setup: one uniform refusal ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_after_setup_is_refused_the_same_way_for_every_body(
    session_factory, monkeypatch
) -> None:
    """FENCE — once a user exists, the answer does not depend on the body.

    A new email, the existing account's email (any case) and the existing
    username all get the same status, body and headers, and nothing is
    created, mailed or audited. Wrong implementation killed: any lookup of the
    body's email or username before the setup check (the pre-TBD-654 409).
    """
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)
    captcha_calls = _captcha_tripwire(monkeypatch)
    sent: list[Any] = []
    monkeypatch.setattr(auth_module, "send_verification_email", lambda *a: sent.append(a))

    app = _make_app(session_factory)
    with TestClient(app) as client:
        answers = [
            client.post(REGISTER, json=_body(username="newuser", email="new@example.com")),
            client.post(REGISTER, json=_body(username="newuser", email="Seed@Example.com")),
            client.post(REGISTER, json=_body(username="seed", email="other@example.com")),
        ]

    for res in answers:
        assert res.status_code == 403, res.text
        assert res.json() == {
            "detail": {
                "code": "setup_complete",
                "message": "This install is already set up. Sign up from the sign-up page.",
            }
        }
    drop = {"date", "x-request-id"}
    headers = [{k: v for k, v in r.headers.items() if k not in drop} for r in answers]
    assert headers[0] == headers[1] == headers[2]
    assert await _count_users(session_factory) == 1
    assert captcha_calls == [] and sent == []
    assert await _all_audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_register_divergent_state_is_refused(session_factory, monkeypatch) -> None:
    """FENCE — users present but no superadmin is NOT the bootstrap.

    The state that separates the retired TBD-365 predicate
    (``count(is_superadmin) == 0``) from ``user_count == 0``. Pre-TBD-365 a
    public signup here received superadmin; pre-TBD-654 it got a plain
    account. Now it gets ``setup_complete`` and the table is untouched.
    """
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory, is_superadmin=False)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body(username="newuser", email="new@example.com"))
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "setup_complete"

    async with session_factory() as db:
        total = await db.scalar(select(func.count()).select_from(User))
        supers = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin.is_(True))
        )
    assert (int(total), int(supers)) == (1, 0)


# ── first-user setup ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_first_user_setup_bypasses_captcha(
    session_factory, monkeypatch
) -> None:
    """Bootstrap exemption: when the DB has zero users, /register skips
    the captcha gate so the /setup flow doesn't deadlock the operator
    on a Cloudflare account they don't have yet."""
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    captcha_calls = _captcha_tripwire(monkeypatch)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body())

    assert res.status_code == 201, res.text
    assert captcha_calls == []
    assert await _count_users(session_factory) == 1
    # Founding-members program: every registration is flagged a founder.
    assert res.json()["is_founder"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("captcha_required", [True, False])
async def test_first_user_setup_is_audited_and_flagged(
    session_factory, monkeypatch, captcha_required
) -> None:
    """FENCE — the bootstrap signup is recorded, tied to the account it made.

    Wrong implementations killed: returning without a row, hardcoding
    ``is_first_user: False``, and a hardcoded ``captcha_required`` (asserted
    from both sides, since only that field says which era a row is from).
    """
    monkeypatch.setattr(app_settings, "captcha_required", captcha_required)
    await _seed_default_plan(session_factory)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body())
    assert res.status_code == 201, res.text
    created = res.json()

    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_email == "founder@example.com"
    assert row.actor_user_id == created["id"]
    assert row.target_org_id == created["org_id"]
    assert row.target_org_name == created["org_name"]
    assert row.outcome == "success"
    assert row.detail["method"] == "password"
    assert row.detail["is_first_user"] is True
    # Cold DB, so every first-ness value is True here regardless of predicate.
    assert row.detail["granted_superadmin"] is True
    assert row.detail["captcha_required"] is captcha_required


# ── INF-05: the production bootstrap token ───────────────────────────────────


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "setup_token", SETUP_TOKEN)


@pytest.mark.asyncio
async def test_production_setup_with_the_token_succeeds(session_factory, production) -> None:
    await _seed_default_plan(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body(setup_token=SETUP_TOKEN))
    assert res.status_code == 201, res.text
    assert res.json()["is_superadmin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "", "wrong-token", SETUP_TOKEN + "x", SETUP_TOKEN[:-1]])
async def test_production_setup_without_the_right_token_is_refused(
    session_factory, production, supplied
) -> None:
    await _seed_default_plan(session_factory)
    body = _body() if supplied is None else _body(setup_token=supplied)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=body)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "setup_token_invalid"
    assert await _count_users(session_factory) == 0
    assert await _all_audit_rows(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, ""])
async def test_production_setup_is_refused_while_the_token_is_unset(
    session_factory, monkeypatch, log_lines, supplied
) -> None:
    """An unset SETUP_TOKEN never means "open": an empty supplied value must
    not match an empty setting. The operator sees why in the log."""
    monkeypatch.setattr(app_settings, "app_env", "production")
    monkeypatch.setattr(app_settings, "setup_token", "")
    await _seed_default_plan(session_factory)
    body = _body() if supplied is None else _body(setup_token=supplied)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=body)
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "setup_token_invalid"
    assert await _count_users(session_factory) == 0
    assert any(e["msg"] == "auth.setup_token_unset" for e in log_lines())


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["development", "test"])
async def test_non_production_setup_skips_the_token(
    session_factory, monkeypatch, app_env
) -> None:
    monkeypatch.setattr(app_settings, "app_env", app_env)
    monkeypatch.setattr(app_settings, "setup_token", SETUP_TOKEN)
    await _seed_default_plan(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body())
    assert res.status_code == 201, res.text


@pytest.mark.asyncio
async def test_production_after_setup_answers_setup_complete_not_the_token_error(
    session_factory, production
) -> None:
    """The token check runs only on an empty install, so a live production
    install answers every caller the same ``setup_complete``."""
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body(username="newuser", email="new@example.com"))
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["code"] == "setup_complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["staging", "Production", "prod", ""])
async def test_any_app_env_but_development_and_test_needs_the_token(
    session_factory, monkeypatch, app_env
) -> None:
    """FENCE — an allowlist, not ``== "production"``.

    Wrong implementation killed: skipping the token unless APP_ENV is exactly
    "production", which an unset, misspelled or new value walks around.
    """
    monkeypatch.setattr(app_settings, "app_env", app_env)
    monkeypatch.setattr(app_settings, "setup_token", SETUP_TOKEN)
    await _seed_default_plan(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/status").json()["setup_token_required"] is True
        refused = client.post(REGISTER, json=_body())
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["code"] == "setup_token_invalid"
        assert await _count_users(session_factory) == 0
        ok = client.post(REGISTER, json=_body(setup_token=SETUP_TOKEN))
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_the_first_user_check_and_commit_run_under_the_bootstrap_lock(
    session_factory, monkeypatch
) -> None:
    """FENCE — the count and the user's commit sit inside one lock hold.

    Wrong implementations killed: counting before taking the lock, or
    releasing it before the commit; either lets two concurrent setups both
    count zero users and both mint a superadmin. The real lock is MySQL's
    ``GET_LOCK``, fenced in ``test_register_bootstrap_lock_mysql.py``.
    """
    from contextlib import asynccontextmanager

    events: list[str] = []
    real_count = auth_module._is_first_user_setup

    async def _count(db):
        events.append("count")
        return await real_count(db)

    @asynccontextmanager
    async def _lock(db):
        events.append("acquire")
        yield
        events.append(f"release users={await _count_users(session_factory)}")

    monkeypatch.setattr(auth_module, "_is_first_user_setup", _count)
    monkeypatch.setattr(auth_module, "_bootstrap_lock", _lock)
    await _seed_default_plan(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body())
    assert res.status_code == 201, res.text
    assert events == ["acquire", "count", "release users=1"]


@pytest.mark.asyncio
async def test_the_bootstrap_lock_is_a_no_op_off_mysql(session_factory) -> None:
    async with session_factory() as db:
        async with auth_module._bootstrap_lock(db):
            pass


def test_the_token_compare_is_constant_time() -> None:
    """Structural: the compare is ``hmac.compare_digest``, never ``==``."""
    import inspect

    src = inspect.getsource(auth_module._setup_token_ok)
    assert "compare_digest(" in src
    assert "== expected" not in src and "expected ==" not in src


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_env", "seed_user", "expected"),
    [
        ("production", False, True),
        ("production", True, False),
        ("development", False, False),
        ("test", False, False),
    ],
)
async def test_status_says_a_token_is_required_only_while_production_needs_setup(
    session_factory, monkeypatch, app_env, seed_user, expected
) -> None:
    """``setup_token_required`` is never true once setup is done, and it does
    not depend on whether SETUP_TOKEN is set, so it reveals nothing beyond
    ``needs_setup``."""
    monkeypatch.setattr(app_settings, "app_env", app_env)
    await _seed_default_plan(session_factory)
    if seed_user:
        await _seed_existing_user(session_factory)
    app = _make_app(session_factory)
    with TestClient(app) as client:
        for token in ("", SETUP_TOKEN):
            monkeypatch.setattr(app_settings, "setup_token", token)
            body = client.get("/api/v1/auth/status").json()
            assert body["setup_token_required"] is expected
            assert body["needs_setup"] is (not seed_user)


# ── TBD-291: `request_id` must be the SANITIZED value ────────────────────────
#
# `RequestContextMiddleware` length-caps (64) and character-set-validates the
# inbound `X-Request-Id` and generates a fresh UUID4 when it fails — but it
# does NOT rewrite the header, so a handler reading `request.headers` gets the
# raw attacker-controlled string and bypasses the trust boundary.
# `audit_events.request_id` is `String(64)` and `record_audit_event` swallows
# every exception, so on MySQL an oversized header meant: account created and
# committed, audit INSERT rejected by strict mode, exception swallowed, 201
# returned with NO row. A client could therefore delete itself from whichever
# side of the refusal rate it was on.
#
# SQLite does not enforce VARCHAR length, so the dropped row cannot be
# reproduced here. These fence the PROPERTY that prevents it instead: the
# recorded id is the sanitized value, never the raw header. The middleware
# echoes the sanitized value back as the response `X-Request-Id`, which gives
# an exact expected value rather than a shape assertion.

# 200 chars: blows the 64-char cap. Also contains no character `_SAFE_ID_RE`
# forbids, so length alone is what rejects it.
_OVERSIZED_REQUEST_ID = "z" * 200
# Within the cap but contains characters outside `[\w.\-]`.
_ILLEGAL_CHAR_REQUEST_ID = "abc$def spa/ce"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_header", [_OVERSIZED_REQUEST_ID, _ILLEGAL_CHAR_REQUEST_ID]
)
async def test_success_audit_records_sanitized_request_id(
    session_factory, raw_header
) -> None:
    """FENCE — the success row carries the middleware's id, not the header.

    Wrong implementation killed: ``request_id=request.headers.get(
    "x-request-id")``, which stores ``raw_header`` verbatim.
    """
    await _seed_default_plan(session_factory)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(REGISTER, json=_body(), headers={"X-Request-Id": raw_header})
    assert res.status_code == 201, res.text

    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1
    recorded = rows[0].request_id
    assert recorded != raw_header, (
        "the raw inbound X-Request-Id reached audit_events; "
        "RequestContextMiddleware rejected it and the handler read it anyway"
    )
    # The sanitized value the middleware actually bound, echoed on the way out.
    assert recorded == res.headers["x-request-id"]
    assert len(recorded) <= 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_header", [_OVERSIZED_REQUEST_ID, _ILLEGAL_CHAR_REQUEST_ID]
)
async def test_captcha_failed_audit_records_sanitized_request_id(
    session_factory, monkeypatch, raw_header
) -> None:
    """FENCE — the REFUSAL row has the same trust boundary.

    The captcha refusal moved to ``/signup/start`` with TBD-654 and kept the
    ``auth.register.captcha_failed`` event, so the fence follows it there.

    Wrong implementation killed: ``request_id=request.headers.get(
    "x-request-id")`` on the ``auth.register.captcha_failed`` call.
    """
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory)

    async def _rejected(token, remote_ip):
        return CaptchaVerifyResult(ok=False, reason=REASON_PROVIDER_REJECTED)

    monkeypatch.setattr(auth_module, "verify_captcha", _rejected)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/signup/start",
            json={"email": "new@example.com", "captcha_token": "tok"},
            headers={"X-Request-Id": raw_header},
        )
    assert res.status_code == 400, res.text

    rows = await _captcha_failed_audit_rows(session_factory)
    assert len(rows) == 1
    recorded = rows[0].request_id
    assert recorded != raw_header, (
        "the raw inbound X-Request-Id reached audit_events; "
        "RequestContextMiddleware rejected it and the handler read it anyway"
    )
    assert recorded == res.headers["x-request-id"]
    assert len(recorded) <= 64
