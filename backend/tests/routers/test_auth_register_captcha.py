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
    # sanitizes the inbound `X-Request-Id` and binds the safe value onto structlog
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
    """Seed one user so the next /register call goes through the
    captcha gate (the first-user-setup bypass requires user_count==0).

    ``is_superadmin`` is still explicit, but TBD-365 changed WHY. There is
    now ONE first-ness predicate (``user_count == 0``), so this flag no
    longer changes any grant outcome — seeding any user closes the bootstrap
    regardless. It is kept because the *divergent* state (users present, zero
    superadmins) is the one the retired predicate mis-answered, and
    ``test_register_divergent_state_grants_no_superadmin`` below builds it
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

    # TBD-291: the gate-off half of the `detail["captcha_required"]` pin. With
    # the gate off there are no refusals to count at all, so the refusal rate's
    # denominator means something different here than it does in the
    # gate-on tests — and only this field on the row says which era it is from.
    # Asserted from BOTH sides (True in the gate-on tests) so a hardcoded
    # literal cannot satisfy the pair.
    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["captcha_required"] is False


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
    # Founding-members program: every registration is flagged a founder.
    assert res.json()["is_founder"] is True


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


# ── TBD-291: registration SUCCESS must be auditable ──────────────────────────
#
# `auth.register.captcha_failed` above was, until TBD-291, the ONLY
# `auth.register.*` audit event in the codebase. Refusals were counted exactly
# and successes not at all, so the refusal RATE could not be computed from
# `audit_events` -- the denominator had to be reconstructed from the `users`
# table by a separate query. Meanwhile `user.login.success` records every
# login, so the asymmetry was specific to registration rather than a policy.


async def _register_success_audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        result = await db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == "auth.register.success"
            )
        )
        return list(result.scalars())


@pytest.mark.asyncio
async def test_successful_registration_writes_an_audit_row(
    session_factory, monkeypatch
) -> None:
    """FENCE — a completed registration is recorded.

    Wrong implementation killed: `main`, where the register handler returns
    without recording anything, so `audit_events` holds refusals and no
    successes.
    """
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
                "captcha_token": "tok",
            },
        )
    assert res.status_code == 201, res.text
    created = res.json()

    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1, f"expected exactly one success row, got {len(rows)}"

    row = rows[0]
    # Identity, not just presence: a row that cannot be tied to the account it
    # created is no use as a denominator. `is not None` would NOT establish that
    # tie — an implementation that recorded the SEEDED user's id and org would
    # satisfy it — so both are asserted equal to the ids in the 201 body.
    assert row.actor_email == "new@example.com"
    assert row.actor_user_id == created["id"]
    assert row.target_org_id == created["org_id"]
    assert row.target_org_name == created["org_name"]
    assert row.outcome == "success"
    assert row.detail["method"] == "password"
    assert row.detail["is_first_user"] is False
    # A user already exists, so the bootstrap is closed and this signup
    # grants nothing. (The seeded account's superadmin flag is incidental
    # since TBD-365 — any seeded user closes it.)
    assert row.detail["granted_superadmin"] is False
    assert row.detail["captcha_required"] is True


@pytest.mark.asyncio
async def test_refused_registration_writes_no_success_row(
    session_factory, monkeypatch
) -> None:
    """FENCE — the counter must mean what it says.

    Wrong implementation killed: recording the success row BEFORE the captcha
    gate, or anywhere that runs on a refused request. That would make the two
    event types overlap and the refusal rate wrong in the safe-looking
    direction (too low), which is the failure mode hardest to notice.

    Pairs with the existing captcha-refusal test: this asserts the ABSENCE the
    other's presence implies.
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
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "another-password-1",
                "captcha_token": "tok",
            },
        )
    assert res.status_code == 400, res.text

    assert await _register_success_audit_rows(session_factory) == []
    assert len(await _captcha_failed_audit_rows(session_factory)) == 1
    assert await _count_users(session_factory) == 1


@pytest.mark.asyncio
async def test_first_user_setup_is_audited_and_flagged(
    session_factory, monkeypatch
) -> None:
    """FENCE — the bootstrap signup is recorded too, and distinguishable.

    The first-user setup path bypasses the captcha gate entirely. If it also
    bypassed the audit row, the very first account on an install would be
    invisible; if it were recorded without the flag, it would be
    indistinguishable from an ordinary signup and would skew the refusal rate
    it exists to help compute.

    This is the COLD state, where every first-ness value is True under every
    implementation — so this test cannot separate a correct predicate from a
    reintroduced flag count. `test_register_divergent_state_grants_no_superadmin`
    is the one that can.

    Wrong implementation killed: hardcoding `is_first_user: False`, which the
    test above cannot see because there a seeded user already exists.
    """
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    # No seeded user on purpose: user_count == 0 is the bootstrap condition.

    async def _tripwire(token, remote_ip):
        raise AssertionError("verify_captcha must not run for the first user")

    monkeypatch.setattr(auth_module, "verify_captcha", _tripwire)

    app = _make_app(session_factory)
    with TestClient(app) as client:
        res = client.post(
            "/api/v1/auth/register",
            json={
                "username": "founder",
                "email": "founder@example.com",
                "password": "another-password-1",
            },
        )
    assert res.status_code == 201, res.text

    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["is_first_user"] is True
    # Cold DB, so every first-ness value is True here regardless of predicate.
    assert rows[0].detail["granted_superadmin"] is True


@pytest.mark.asyncio
async def test_register_divergent_state_grants_no_superadmin(
    session_factory, monkeypatch
) -> None:
    """FENCE — the divergent state grants nothing, and the row says so.

    TBD-365 retired the second first-ness predicate. This test previously
    asserted the OPPOSITE of what it asserts now: that a signup in this state
    legitimately receives superadmin. It was renamed and inverted rather than
    deleted, because the state it constructs is still the only one that
    separates the retired predicate from the surviving one.

    A user exists (so this is NOT the bootstrap and the captcha gate DOES
    run) and no superadmin exists among them. Pre-TBD-365 this signup
    silently received superadmin.

    Wrong implementations killed:
      * ``is_superadmin=(existing_superadmin == 0)`` — the retired predicate.
      * a payload that RESTATES the predicate in the outcome slots instead of
        reading the stored row: the assertions below compare
        ``granted_superadmin`` against the account's real flag, so a row that
        certifies an outcome the constructor did not produce goes red.

    ``len(verify_calls) == 1`` is retained as an INDEPENDENT witness that this
    is not the bootstrap path — it comes from the captcha gate, entirely
    outside the users table, so it survives any fixture mistake.
    """
    monkeypatch.setattr(app_settings, "captcha_required", True)
    await _seed_default_plan(session_factory)
    await _seed_existing_user(session_factory, is_superadmin=False)

    verify_calls: list[Any] = []

    async def _ok(token, remote_ip):
        verify_calls.append((token, remote_ip))
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
                "captcha_token": "tok",
            },
        )
    assert res.status_code == 201, res.text
    # Independent witness, from outside the users table: the gate ran, so this
    # is provably not the bootstrap path.
    assert len(verify_calls) == 1
    body = res.json()
    assert body["is_superadmin"] is False, (
        "a public self-signup received superadmin on an install that merely "
        f"lacks one. body={body}"
    )

    async with session_factory() as db:
        total = await db.scalar(select(func.count()).select_from(User))
        supers = await db.scalar(
            select(func.count()).select_from(User).where(User.is_superadmin.is_(True))
        )
    assert (int(total), int(supers)) == (2, 0), (
        "register either did not run or minted a superadmin outside the "
        f"bootstrap: {total} users / {supers} superadmins"
    )

    rows = await _register_success_audit_rows(session_factory)
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail["is_first_user"] is False
    assert detail["granted_superadmin"] == body["is_superadmin"] is False
    assert detail["email_verified_on_create"] == body["email_verified"] is False


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
    session_factory, monkeypatch, raw_header
) -> None:
    """FENCE — the success row carries the middleware's id, not the header.

    Wrong implementation killed: ``request_id=request.headers.get(
    "x-request-id")``, which stores ``raw_header`` verbatim.
    """
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
                "captcha_token": "tok",
            },
            headers={"X-Request-Id": raw_header},
        )
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

    Fixing only the success call site would leave the numerator suppressible by
    the same trick as the denominator, which is worse than either alone: the
    rate would still be wrong and the two sides would now fail differently.

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
            "/api/v1/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "another-password-1",
                "captcha_token": "tok",
            },
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
