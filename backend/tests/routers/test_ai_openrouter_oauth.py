"""TBD-632 PR 1: connect OpenRouter with OAuth PKCE (spec O1-O21).

Real routers behind the real ``get_current_user``; only the DB is isolated and
the outbound calls are faked with an injected transport. Only
``POST /api/v1/auth/keys`` calls are counted (create's validate is stubbed).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import state_db
from app.config import settings
from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.org_ai_credential import AiProvider, OrgAICredential
from app.models.user import Organization, Role, User
from app.routers.ai_providers import router as ai_providers_router
from app.security import create_access_token, decode_refresh_jti_sid, derive_hmac_key
from app.services import ai_credential_service
from app.services.ai_providers import openrouter_oauth
from app.services.ai_providers.base import ValidateResult
from app.services.ai_providers.openai_compatible import OPENROUTER_BASE_URL
from app.services.api_token_service import hash_api_token
from tests.conftest import issue_test_refresh_token
from tests.factories import make_test_app

BASE = "/api/v1/settings/ai-providers/oauth/openrouter"
KEY = "sk-or-v1-" + "a1b2c3d4" * 6  # the minted key; must never leak
APP_URL = "https://app.example.test"
CODE = "auth-code-123"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _new_nonce() -> str:
    return _b64u(os.urandom(24))


def _nh(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def _sid_h(sid: str) -> str:
    return hmac.new(derive_hmac_key(b"openrouter-oauth-sid-v1"), sid.encode(), "sha256").hexdigest()


def _verifier(jti: str) -> str:
    return _b64u(hmac.new(derive_hmac_key(b"openrouter-pkce-v1"), jti.encode(), "sha256").digest())


class Fake:
    """Injected OpenRouter transport; records only auth/keys calls."""

    def __init__(self):
        self.calls: list[dict] = []
        self.respond = lambda req: httpx.Response(200, json={"key": KEY})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/keys" and request.method == "POST":
            self.calls.append(json.loads(request.content))
        return self.respond(request)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(openrouter_oauth, "TRANSPORT", httpx.MockTransport(f.handler))
    return f


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(
        settings, "ai_credential_encryption_key", base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    )
    monkeypatch.setattr(settings, "ai_credential_encryption_key_prev", "")
    monkeypatch.setattr(settings, "app_url", APP_URL)

    async def _ok(**_kw):
        return ValidateResult(ok=True, discovered_models=["m"], discovered_capabilities=["chat"])

    monkeypatch.setattr(ai_credential_service, "_run_validate", _ok)


@pytest.fixture
async def factory(tmp_path):
    # File-backed, not a shared StaticPool connection: O5 runs two requests concurrently.
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/o.db", connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


async def _mk_user(s, org, name, role, **kw):
    u = User(
        org_id=org.id, username=name, email=f"{name}@x.io", password_hash="h",
        role=role, is_active=True, email_verified=True, **kw,
    )
    s.add(u)
    await s.flush()
    return u


@pytest.fixture
async def world(factory):
    async with factory() as s:
        org_a = Organization(name="A", billing_cycle_day=1)
        org_b = Organization(name="B", billing_cycle_day=1)
        s.add_all([org_a, org_b])
        await s.flush()
        owner = await _mk_user(s, org_a, "owner", Role.OWNER, is_superadmin=True)
        admin2 = await _mk_user(s, org_a, "admin2", Role.ADMIN, is_superadmin=True)
        member = await _mk_user(s, org_a, "member", Role.MEMBER)
        await s.commit()
        ids = {"owner": owner.id, "admin2": admin2.id, "member": member.id, "org_a": org_a.id, "org_b": org_b.id}
    return ids


@pytest.fixture
def app(factory):
    from app.rate_limit import limiter

    application = make_test_app(factory, routers=[ai_providers_router], override_session_factory=True)
    application.state.limiter = limiter
    return application


class Sess:
    """One logged-in browser: access token + refresh cookie."""

    def __init__(self, app, user_id, org_id, role, *, cookie_of: "Sess | None" = None):
        self.user_id = user_id
        self.refresh = cookie_of.refresh if cookie_of else issue_test_refresh_token(user_id)
        self.sid = decode_refresh_jti_sid(self.refresh)[1]
        self.access = create_access_token(user_id, org_id, role)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t",
            headers={"Authorization": f"Bearer {self.access}"},
        )
        self.client.cookies.set("refresh_token", self.refresh)

    async def start(self, nonce: str):
        return await self.client.post(f"{BASE}/start", json={"nonce_hash": _nh(nonce)})

    async def complete(self, state, nonce, code=CODE):
        return await self.client.post(f"{BASE}/complete", json={"code": code, "state": state, "nonce": nonce})

    async def flow(self, nonce=None):
        nonce = nonce or _new_nonce()
        r = await self.start(nonce)
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        return nonce, url, parse_qs(urlparse(url).query)["state"][0]


@pytest.fixture
async def owner(app, world):
    s = Sess(app, world["owner"], world["org_a"], "owner")
    yield s
    await s.client.aclose()


def _claims(state: str) -> dict:
    return jwt.decode(state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def _sign(owner: Sess, world, nonce, **over) -> str:
    now = int(time.time())
    claims = {
        "type": "openrouter_oauth", "sub": str(owner.user_id), "org_id": world["org_a"],
        "sid_h": _sid_h(owner.sid), "nh": _nh(nonce), "jti": secrets.token_urlsafe(16),
        "iat": now, "exp": now + 600,
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def _audits(factory, event_type="ai.credential.oauth_refused"):
    async with factory() as s:
        return list((await s.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))).all())


async def _rows(factory):
    async with factory() as s:
        return list((await s.scalars(select(OrgAICredential))).all())


async def _mint_pat(factory, user_id, email):
    plaintext = "pat_" + secrets.token_urlsafe(32)
    async with factory() as s:
        s.add(ApiToken(
            token_hash=hash_api_token(plaintext), token_prefix=plaintext[:14], name="t", scope="write",
            created_by_user_id=user_id, created_by_email=email,
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
        ))
        await s.commit()
    return plaintext


# ── O1 ──────────────────────────────────────────────────────────────────────
async def test_o1_start_url_pkce_and_no_secret_in_state(owner):
    nonce = _new_nonce()
    r = await owner.start(nonce)
    assert r.status_code == 200
    url = r.json()["url"]
    q = parse_qs(urlparse(url).query)
    state = q["state"][0]
    jti = _claims(state)["jti"]
    verifier = _verifier(jti)
    assert q["code_challenge"][0] == _b64u(hashlib.sha256(verifier.encode()).digest())
    assert q["code_challenge_method"] == ["S256"]
    assert q["callback_url"] == [APP_URL + "/settings/ai-providers/openrouter/callback"]
    assert q["key_label"] == ["The Better Decision (" + jti[:8] + ")"]
    assert url.startswith("https://openrouter.ai/auth?")
    for secret in (verifier, owner.sid, nonce, owner.refresh):
        assert secret not in url and secret not in r.text
        for v in _claims(state).values():
            assert secret not in str(v)


# ── O2 / O3 ─────────────────────────────────────────────────────────────────
async def test_o2_complete_happy_path(owner, fake, factory):
    nonce, url, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 201, r.text
    assert len(fake.calls) == 1
    call = fake.calls[0]
    ch = parse_qs(urlparse(url).query)["code_challenge"][0]
    assert set(call) == {"code", "code_verifier", "code_challenge_method"}
    assert call["code"] == CODE and call["code_challenge_method"] == "S256"
    assert _b64u(hashlib.sha256(call["code_verifier"].encode()).digest()) == ch
    (row,) = await _rows(factory)
    assert row.provider == AiProvider.OPENAI_COMPATIBLE
    assert row.base_url == OPENROUTER_BASE_URL
    assert row.label == "OpenRouter (" + _claims(state)["jti"][:8] + ")"


async def _assert_no_key(factory, caplog, *bodies):
    blob = "".join(bodies) + caplog.text
    for e in await _audits(factory) + await _audits(factory, "ai.credential.created"):
        blob += json.dumps(e.detail)
    assert KEY not in blob


async def test_o3_key_never_leaks_happy(owner, fake, factory, caplog):
    caplog.set_level(logging.DEBUG)
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 201
    await _assert_no_key(factory, caplog, r.text)


async def test_o3_key_never_leaks_bad_key_and_create_failures(owner, fake, factory, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    bodies = []
    fake.respond = lambda req: httpx.Response(200, json={"key": KEY + " " * 5000})  # over the max
    nonce, _, state = await owner.flow()
    bodies.append((await owner.complete(state, nonce)).text)

    fake.respond = lambda req: httpx.Response(200, json={"key": KEY})

    async def _boom(*a, **k):
        raise RuntimeError("unexpected: " + KEY)

    monkeypatch.setattr(ai_credential_service, "create_credential", _boom)
    nonce, _, state = await owner.flow()
    bodies.append((await owner.complete(state, nonce)).text)
    await _assert_no_key(factory, caplog, *bodies)


# ── O4 / O5 ─────────────────────────────────────────────────────────────────
async def test_o4_sequential_replay(owner, fake, factory):
    nonce, _, state = await owner.flow()
    assert (await owner.complete(state, nonce)).status_code == 201
    r = await owner.complete(state, nonce)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "oauth_state_used"
    assert len(fake.calls) == 1 and len(await _rows(factory)) == 1


async def test_o5_concurrent_replay(owner, fake, factory):
    nonce, _, state = await owner.flow()
    a, b = await asyncio.gather(owner.complete(state, nonce), owner.complete(state, nonce))
    assert sorted([a.status_code, b.status_code]) == [201, 409]
    assert len(fake.calls) == 1 and len(await _rows(factory)) == 1


# ── O6-O10: binding ─────────────────────────────────────────────────────────
async def _mismatch(r, fake, factory):
    assert r.status_code == 409 and r.json()["detail"]["code"] == "oauth_state_mismatch", r.text
    assert fake.calls == []
    reasons = [e.detail["reason"] for e in await _audits(factory)]
    assert reasons == ["state_mismatch"]


async def test_o6_other_user_same_cookie(app, world, owner, fake, factory):
    nonce, _, state = await owner.flow()
    other = Sess(app, world["admin2"], world["org_a"], "admin", cookie_of=owner)
    try:
        await _mismatch(await other.complete(state, nonce), fake, factory)
    finally:
        await other.client.aclose()
    # the stranger did not burn the owner's state
    assert (await owner.complete(state, nonce)).status_code == 201


async def test_o7_other_org(owner, world, fake, factory):
    nonce = _new_nonce()
    state = _sign(owner, world, nonce, org_id=world["org_b"])
    await _mismatch(await owner.complete(state, nonce), fake, factory)


async def test_o8_other_sid(app, world, owner, fake, factory):
    nonce, _, state = await owner.flow()
    owner.client.cookies.set("refresh_token", issue_test_refresh_token(world["owner"]))
    await _mismatch(await owner.complete(state, nonce), fake, factory)


async def test_o9_family_revoked(owner, fake, factory):
    nonce, _, state = await owner.flow()
    assert await state_db.session_revoke_family(owner.sid)
    await _mismatch(await owner.complete(state, nonce), fake, factory)


async def test_o10_cutoff(owner, world, fake, factory):
    nonce = _new_nonce()
    t = int(time.time()) - 10
    state = _sign(owner, world, nonce, iat=t, exp=t + 600)
    async with factory() as s:
        u = await s.get(User, world["owner"])
        u.sessions_invalidated_at = datetime.fromtimestamp(t + 5, timezone.utc).replace(tzinfo=None)
        await s.commit()
    # fresh access token whose iat is after the cutoff, same cookie and sid
    now = int(time.time())
    owner.client.headers["Authorization"] = "Bearer " + jwt.encode(
        {"sub": str(world["owner"]), "org_id": world["org_a"], "role": "owner", "type": "access",
         "iat": now, "exp": now + 600},
        settings.jwt_secret_key, algorithm=settings.jwt_algorithm,
    )
    await _mismatch(await owner.complete(state, nonce), fake, factory)


# ── O11 ─────────────────────────────────────────────────────────────────────
async def test_o11_invalid_states(owner, world, fake, factory):
    nonce = _new_nonce()
    good = _sign(owner, world, nonce)
    now = int(time.time())
    bad = {
        "tampered": good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"),
        "expired": _sign(owner, world, nonce, iat=now - 700, exp=now - 100),
        "wrong_type": _sign(owner, world, nonce, type="access"),
    }
    for name, state in bad.items():
        r = await owner.complete(state, nonce)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "oauth_state_invalid", name
    assert fake.calls == []
    assert [e.detail["reason"] for e in await _audits(factory)] == ["state_invalid"] * 3


# ── O12 ─────────────────────────────────────────────────────────────────────
async def test_o12_nonce(owner, fake, factory):
    nonce, _, state = await owner.flow()
    await _mismatch(await owner.complete(state, _new_nonce()), fake, factory)
    other_nonce, _, other_state = await owner.flow()
    r = await owner.complete(state, other_nonce)
    assert r.status_code == 409 and fake.calls == []


# ── O13 / O14 / O15 / O16 ───────────────────────────────────────────────────
async def test_o13_rejected_and_busy(owner, fake, factory):
    fake.respond = lambda req: httpx.Response(403, json={"error": "bad"})
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "openrouter_code_rejected"
    fake.respond = lambda req: httpx.Response(429)
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "openrouter_busy"
    assert [e.detail["reason"] for e in await _audits(factory)] == ["exchange_rejected"] * 2
    assert await _rows(factory) == []


def _timeout(req):
    raise httpx.ReadTimeout("slow")


@pytest.mark.parametrize("name,respond", [
    ("500", lambda r: httpx.Response(500)),
    ("timeout", _timeout),
    ("oversize", lambda r: httpx.Response(200, content=b"x" * 70_000)),
    ("nonjson", lambda r: httpx.Response(200, content=b"nope")),
    ("int_key", lambda r: httpx.Response(200, json={"key": 5})),
    ("short_key", lambda r: httpx.Response(200, json={"key": "abc"})),
    ("blank_key", lambda r: httpx.Response(200, json={"key": "    "})),
    ("long_key", lambda r: httpx.Response(200, json={"key": "k" * 4097})),
    ("302", lambda r: httpx.Response(302, headers={"location": "https://evil.test/"})),
    ("201", lambda r: httpx.Response(201, json={"key": KEY})),
])
async def test_o14_orphan_possible(owner, fake, factory, name, respond):
    fake.respond = respond
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 502, (name, r.text)
    assert r.json()["detail"]["code"] == "openrouter_unavailable"
    (a,) = await _audits(factory)
    assert a.detail["reason"] == "orphan_possible" and a.detail["ref"] == _claims(state)["jti"][:8]
    assert await _rows(factory) == []


async def test_o15_validation_failed(owner, fake, factory, monkeypatch):
    async def _bad(**_kw):
        return ValidateResult(ok=False, error="nope")

    monkeypatch.setattr(ai_credential_service, "_run_validate", _bad)
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "credential_validation_failed"
    (a,) = await _audits(factory)
    assert a.detail["reason"] == "validation_failed"
    assert await _rows(factory) == []


async def test_o16_unexpected_create_error_with_key(owner, fake, factory, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def _boom(*a, **k):
        raise RuntimeError("unexpected: " + KEY)

    monkeypatch.setattr(ai_credential_service, "create_credential", _boom)
    nonce, _, state = await owner.flow()
    r = await owner.complete(state, nonce)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "openrouter_unavailable"
    (a,) = await _audits(factory)
    assert a.detail["reason"] == "orphan_possible"
    await _assert_no_key(factory, caplog, r.text)


async def test_cancel_after_send_audits_orphan(owner, fake, factory, monkeypatch):
    """A client disconnect (anyio scope cancel) after the send still audits, shielded."""
    started = asyncio.Event()

    async def _hang(*a, **k):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(ai_credential_service, "create_credential", _hang)
    nonce, _, state = await owner.flow()
    scopes: list[anyio.CancelScope] = []

    async def run():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await owner.complete(state, nonce)

    task = asyncio.create_task(run())
    await started.wait()
    scopes[0].cancel()
    await task
    assert [e.detail["reason"] for e in await _audits(factory)] == ["orphan_possible"]


@pytest.mark.parametrize("body", [
    {"key": 5}, {"key": "abc"}, {"key": "    "}, {"key": "k" * 4097}, [], "str", {},
])
async def test_o14_exchange_helper_refuses_bad_key(monkeypatch, body):
    """The helper itself must refuse these; downstream validation would also 502 them."""
    monkeypatch.setattr(
        openrouter_oauth, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    with pytest.raises(openrouter_oauth.ExchangeFailed):
        await openrouter_oauth.exchange_code(CODE, "v" * 43)


# ── O17 ─────────────────────────────────────────────────────────────────────
async def test_o17_gates(app, world, owner, fake, factory):
    nonce, _, state = await owner.flow()
    for uid, email in ((world["owner"], "owner@x.io"), (world["admin2"], "admin2@x.io")):
        pat = await _mint_pat(factory, uid, email)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                              headers={"Authorization": f"Bearer {pat}"})
        c.cookies.set("refresh_token", owner.refresh)
        try:
            assert (await c.post(f"{BASE}/start", json={"nonce_hash": _nh(nonce)})).status_code == 403
            r = await c.post(f"{BASE}/complete", json={"code": CODE, "state": state, "nonce": nonce})
            assert r.status_code == 403
        finally:
            await c.aclose()
    member = Sess(app, world["member"], world["org_a"], "member")
    try:
        assert (await member.start(nonce)).status_code == 403
        assert (await member.complete(state, nonce)).status_code == 403
    finally:
        await member.client.aclose()
    assert fake.calls == []


# ── O18 ─────────────────────────────────────────────────────────────────────
async def test_o18_rate_limits(app, world, owner, fake, factory):
    nonce = _new_nonce()
    for _ in range(10):
        assert (await owner.start(nonce)).status_code == 200
    r = await owner.start(nonce)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "oauth_rate_limited"
    other = Sess(app, world["admin2"], world["org_a"], "admin")
    try:
        assert (await other.start(nonce)).status_code == 200
    finally:
        await other.client.aclose()
    for _ in range(10):
        assert (await owner.complete("garbage", nonce)).status_code == 400
    assert (await owner.complete("garbage", nonce)).status_code == 429
    assert len(await _audits(factory)) == 10


# ── guards O19-O21 ──────────────────────────────────────────────────────────
async def test_o19_no_or_bad_cookie(owner):
    owner.client.cookies.delete("refresh_token")
    r = await owner.start(_new_nonce())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "oauth_session_unavailable"
    owner.client.cookies.set("refresh_token", "not-a-jwt")
    r = await owner.start(_new_nonce())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "oauth_session_unavailable"


async def test_o20_family_check_db_error(owner, fake, monkeypatch):
    nonce, _, state = await owner.flow()

    async def _boom(sid):
        raise SQLAlchemyError("down")

    monkeypatch.setattr(state_db, "session_family_exists", _boom)
    assert (await owner.complete(state, nonce)).status_code == 503
    assert fake.calls == []


async def test_o21_audit_detail_and_org(app, world, owner, fake, factory):
    nonce = _new_nonce()
    state = _sign(owner, world, nonce, org_id=world["org_b"])
    await owner.complete(state, nonce, code="SECRETCODE")
    fake.respond = lambda req: httpx.Response(403)
    n2, _, s2 = await owner.flow()
    await owner.complete(s2, n2, code="SECRETCODE")
    rows = await _audits(factory)
    assert len(rows) == 2
    for e in rows:
        assert e.target_org_id == world["org_a"] and e.outcome == "failure"
        blob = json.dumps(e.detail)
        for secret in ("SECRETCODE", state, s2, nonce, n2, _verifier(_claims(s2)["jti"]), KEY):
            assert secret not in blob
