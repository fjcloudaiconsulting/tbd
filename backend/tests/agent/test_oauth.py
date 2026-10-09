"""TBD-587: the MCP OAuth 2.1 authorization server (``app.routers.oauth``).

Routes run on the REAL app with the REAL ``get_current_user`` seam (only
``get_db`` / ``get_session_factory`` point at an in-memory SQLite), as in
``test_agent_tokens.py``. MySQL-only interleaves live in
``test_oauth_race_mysql.py``.

Fences F-O1..F-O20 (the build spec's table as amended by its sign-off folds);
each docstring names the wrong implementation it kills. Guards: G1 and the
token endpoint's error shapes.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, insert, select, update

from app import rate_limit_db
from app.agent import actions
from app.agent.auth import authenticate_agent_token, www_authenticate
from app.agent.registry import ToolContext
from app.config import settings
from app.database import get_db
from app.deps import get_session_factory
from app.main import app
from app.models.agent_pending_action import AgentPendingAction
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
from app.models.notification import Notification
from app.models.oauth_client import OAuthClient
from app.models.system_setting import SystemSetting
from app.models.user import Role, User
from app.rate_limit import limiter
from app.services import feature_service, notification_service
from tests.agent.test_agent_tokens import (
    PASSWORD,
    _audits,
    _auth,
    _jwt,
    _mint_body,
    _org,
    _set_cutoff,
    _user,
)

UTC = timezone.utc
APP = "https://tbd.example.test"
RESOURCE = APP + "/mcp"
REG = "/api/v1/oauth/register"
CTX = "/api/v1/oauth/authorize/context"
AUTHZ = "/api/v1/oauth/authorize"
TOKEN = "/api/v1/oauth/token"
CB = "https://claude.ai/api/mcp/auth_callback"


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest_asyncio.fixture
async def factory():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.models import Base

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
def _env(monkeypatch):
    limiter.reset()
    monkeypatch.setattr(settings, "app_url", APP + "/")
    monkeypatch.setenv("PFV_RUNTIME", "app_platform")  # trust do-connecting-ip
    monkeypatch.setattr(
        notification_service, "send_notification_email", AsyncMock(return_value=None)
    )
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def client(factory):
    async def _db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_session_factory] = lambda: factory
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


# ── helpers ────────────────────────────────────────────────────────────────


def _ip(ip: str | None) -> dict:
    return {"do-connecting-ip": ip} if ip else {}


async def _register(client, uris=(CB,), name: str | None = "Claude", ip=None, **extra):
    body = {"redirect_uris": list(uris), **extra}
    if name is not None:
        body["client_name"] = name
    return await client.post(REG, json=body, headers=_ip(ip))


async def _cid(client, uris=(CB,), name="Claude") -> str:
    r = await _register(client, uris, name)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _q(url: str) -> dict:
    return dict(parse_qsl(urlsplit(url).query))


def _params(cid: str, challenge: str, *, redirect=CB, scope="agent:write", **over) -> dict:
    p = {
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": scope, "state": "st8", "resource": RESOURCE, **over,
    }
    return {k: v for k, v in p.items() if v is not None}


async def _consent(client, h, cid, *, granted="agent:write", approve=True, ip=None,
                   password=PASSWORD, **over):
    verifier, challenge = _pkce()
    body = {**_params(cid, challenge, **over), "approve": approve,
            "granted_scope": granted, "current_password": password}
    r = await client.post(AUTHZ, json=body, headers={**h, **_ip(ip)})
    return r, verifier


async def _grant(factory, client, *, scope="agent:write", uid=None, cid=None, role=Role.MEMBER,
                 superadmin=False) -> dict:
    if uid is None:
        org = await _org(factory, f"O{secrets.token_hex(3)}")
        uid = await _user(factory, org, f"u{secrets.token_hex(3)}", role=role,
                          superadmin=superadmin)
    h = await _jwt(factory, uid)
    cid = cid or await _cid(client)
    r, verifier = await _consent(client, h, cid, scope=scope, granted=scope)
    assert r.status_code == 200, r.text
    code = _q(r.json()["redirect_to"])["code"]
    async with factory() as s:
        org_id = (await s.get(User, uid)).org_id
    return dict(uid=uid, org=org_id, h=h, cid=cid, v=verifier, code=code)


async def _exchange(client, g, *, ip=None, **over):
    data = {"grant_type": "authorization_code", "code": g["code"], "redirect_uri": CB,
            "client_id": g["cid"], "code_verifier": g["v"], **over}
    data = {k: v for k, v in data.items() if v is not None}
    return await client.post(TOKEN, data=data, headers=_ip(ip))


async def _connected(factory, client, **kw) -> dict:
    g = await _grant(factory, client, **kw)
    r = await _exchange(client, g)
    assert r.status_code == 200, r.text
    return {**g, **r.json()}


async def _refresh(client, rt, *, ip=None, **over):
    data = {"grant_type": "refresh_token", "refresh_token": rt, **over}
    data = {k: v for k, v in data.items() if v is not None}
    return await client.post(TOKEN, data=data, headers=_ip(ip))


def _err(r) -> tuple[int, str]:
    return r.status_code, r.json().get("error")


async def _rows(factory, uid) -> list[ApiToken]:
    async with factory() as s:
        return list((await s.execute(
            select(ApiToken).where(ApiToken.created_by_user_id == uid).order_by(ApiToken.id)
        )).scalars().all())


async def _one(factory, uid) -> ApiToken:
    [row] = await _rows(factory, uid)
    return row


async def _lapse_access(factory, uid) -> None:
    """Access tokens expired an hour ago, refresh still live (between refreshes)."""
    async with factory() as s:
        await s.execute(update(ApiToken).where(ApiToken.created_by_user_id == uid)
                        .values(expires_at=_naive_now() - timedelta(hours=1)))
        await s.commit()


async def _dead(factory, token: str) -> None:
    with pytest.raises(HTTPException) as ei:
        await _auth(factory, token)
    assert ei.value.status_code == 401


def _on_entitlements(monkeypatch, factory, stmt) -> None:
    """Run ``stmt`` in ANOTHER session right after the token endpoint reads its
    row (it reads the entitlements between the read and the UPDATE): simulates
    a concurrent writer landing in that window."""
    real = feature_service.get_entitlements
    fired = []

    async def hooked(db, org_id, **kw):
        if not fired:
            fired.append(1)
            async with factory() as other:
                await other.execute(stmt)
                await other.commit()
        return await real(db, org_id, **kw)

    monkeypatch.setattr(feature_service, "get_entitlements", hooked)


# ── F-O1: PKCE, single use ──────────────────────────────────────────────────


async def test_f_o1_consent_requires_an_s256_challenge(factory, client):
    """FENCE F-O1 (S14). Wrong implementation: PKCE optional (``plain`` or a
    missing challenge accepted at context/authorize). Each refusal is the
    error redirect, carrying ``state`` and ``iss`` (S6)."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    cid = await _cid(client)
    _, challenge = _pkce()
    for over in ({"code_challenge_method": "plain"}, {"code_challenge_method": None},
                 {"code_challenge": None}, {"code_challenge": "short"}):
        params = _params(cid, challenge, **over)
        for r in (await client.get(CTX, params=params, headers=h),
                  await client.post(AUTHZ, json={**params, "approve": True,
                                                 "granted_scope": "agent:read",
                                                 "current_password": PASSWORD}, headers=h)):
            assert r.status_code == 400, (over, r.text)
            detail = r.json()["detail"]
            assert detail["code"] == "invalid_request"
            q = _q(detail["redirect_to"])
            assert (q["error"], q["state"], q["iss"]) == ("invalid_request", "st8", APP)
    assert (await _rows(factory, (await _one_user(factory)).id)) == []
    assert (await client.get(CTX, params=_params(cid, challenge), headers=h)).status_code == 200


async def _one_user(factory) -> User:
    async with factory() as s:
        return (await s.execute(select(User))).scalars().one()


async def test_f_o1_failed_exchanges_change_nothing(factory, client):
    """FENCE F-O1. Wrong implementations: PKCE optional (the missing-verifier
    cell gets tokens); the verifier checked after burning the code (the right
    verifier then fails); redirect or client not bound to the code."""
    g = await _grant(factory, client)
    other = await _cid(client, ["https://other.example/cb"], "Other")
    # Four failures and the success: the per-code bucket is 5/minute.
    for over in ({"code_verifier": None}, {"code_verifier": _pkce()[0]},
                 {"redirect_uri": CB + "x"}, {"client_id": other}):
        r = await _exchange(client, g, **over)
        assert _err(r) == (400, "invalid_grant"), (over, r.text)
        assert g["v"] not in r.text
    row = await _one(factory, g["uid"])
    assert (row.refresh_hash, row.revoked_at) == (None, None)
    r = await _exchange(client, g)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"].startswith("pat_") and body["refresh_token"].startswith("rt_")
    assert (body["token_type"], body["expires_in"], body["scope"]) == ("Bearer", 3600, "agent:write")
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    user, row = await _auth(factory, body["access_token"])
    assert (user.id, row.scope) == (g["uid"], "agent:write")


# ── F-O1b: code replay ──────────────────────────────────────────────────────


async def test_f_o1b_replay_with_the_verifier_revokes_and_without_changes_nothing(factory, client):
    """FENCE F-O1b. Wrong implementations: no reuse detection (the replay
    leaves the grant alive); revoke on ANY replay (an interceptor without the
    verifier kills the user's grant)."""
    g = await _connected(factory, client)
    assert _err(await _exchange(client, g, code_verifier=_pkce()[0])) == (400, "invalid_grant")
    await _auth(factory, g["access_token"])  # still alive
    assert await _audits(factory, "agent_token.revoked") == []
    assert _err(await _exchange(client, g)) == (400, "invalid_grant")
    await _dead(factory, g["access_token"])
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")
    [audit] = await _audits(factory, "agent_token.revoked")
    assert audit.detail["reason"] == "code_reuse"
    assert audit.detail["api_token_id"] == (await _one(factory, g["uid"])).id


# ── F-O2: no redirect before the redirect is trusted ────────────────────────


@pytest.mark.parametrize("case", ["unknown_client", "no_client", "unregistered_redirect"])
async def test_f_o2_untrusted_redirect_is_never_used(factory, client, case):
    """FENCE F-O2. Wrong implementation: redirecting errors to the supplied
    URI before the client and its redirect are verified."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    cid = await _cid(client)
    _, challenge = _pkce()
    over = {"unknown_client": {"client_id": "f" * 32}, "no_client": {"client_id": None},
            "unregistered_redirect": {"redirect_uri": "https://evil.example/cb"}}[case]
    # Every OTHER parameter is also wrong: the client check must come first.
    params = _params(cid, challenge, response_type="token", **over)
    for r in (await client.get(CTX, params=params, headers=h),
              await client.post(AUTHZ, json={**params, "approve": False}, headers=h)):
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "redirect_to" not in detail and "location" not in r.headers
        assert detail["code"] == ("invalid_redirect_uri" if case == "unregistered_redirect"
                                  else "invalid_client")


# ── F-O3: refresh rotates in place ─────────────────────────────────────────


async def test_f_o3_refresh_keeps_the_row_and_its_pending_actions(factory, client):
    """FENCE F-O3. Wrong implementation: a new row per refresh (the pending
    action bound to the old token id is no longer the new token's to decide).
    ``cancel`` shares confirm's principal binding (``actions._mine``)."""
    g = await _connected(factory, client)
    before = await _one(factory, g["uid"])
    aid = secrets.token_hex(16)
    now = _naive_now()
    async with factory() as s:
        s.add(AgentPendingAction(
            id=aid, org_id=g["org"], user_id=g["uid"], channel="mcp", api_token_id=before.id,
            tool="budgets_update_amount", risk="write", mode="confirm", args_json={},
            args_sha256="0" * 64, fingerprint="0" * 64, preview_json={}, status="pending",
            created_at=now, expires_at=now + timedelta(minutes=10),
        ))
        await s.commit()
    r = await _refresh(client, g["refresh_token"])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != g["access_token"] and new["refresh_token"] != g["refresh_token"]
    after = await _one(factory, g["uid"])
    assert (after.id, after.created_at) == (before.id, before.created_at)
    await _dead(factory, g["access_token"])
    user, row = await _auth(factory, new["access_token"])
    async with factory() as s:
        ctx = ToolContext(db=s, user=await s.get(User, user.id), org_id=user.org_id,
                          channel="mcp", api_token_id=row.id)
        assert (await actions.cancel(ctx, aid, scope=row.scope))["status"] == "cancelled"


# ── F-O4: session cutoff ───────────────────────────────────────────────────


@pytest.mark.parametrize("column", ["sessions_invalidated_at", "password_changed_at"])
async def test_f_o4_cutoff_kills_refresh_and_unredeemed_codes(factory, client, column):
    """FENCE F-O4. Wrong implementations: the cutoff checked only at ``/mcp``
    (the token endpoint mints a fresh access token after a sign out
    everywhere); ``created_at`` bumped on refresh/exchange (the refreshed
    grant would then post-date the cutoff and survive it)."""
    g = await _connected(factory, client)
    g2 = await _grant(factory, client, uid=g["uid"], cid=g["cid"])  # unredeemed code
    hour_ago = _naive_now().replace(microsecond=0) - timedelta(hours=1)
    async with factory() as s:
        await s.execute(update(ApiToken).values(created_at=hour_ago))
        await s.commit()
    r = await _refresh(client, g["refresh_token"])
    assert r.status_code == 200, r.text
    assert {row.created_at for row in await _rows(factory, g["uid"])} == {hour_ago}
    await _set_cutoff(factory, g["uid"], column, hour_ago + timedelta(minutes=30))
    assert _err(await _refresh(client, r.json()["refresh_token"])) == (400, "invalid_grant")
    assert _err(await _exchange(client, g2)) == (400, "invalid_grant")
    assert {row.created_at for row in await _rows(factory, g["uid"])} == {hour_ago}


# ── F-O5: refresh reuse detection ──────────────────────────────────────────


async def test_f_o5_rotated_out_refresh_token_revokes_the_grant(factory, client):
    """FENCE F-O5. Wrong implementation: no reuse detection (the old refresh
    token is just refused and the thief's rotated pair lives on)."""
    g = await _connected(factory, client)
    r = await _refresh(client, g["refresh_token"])
    assert r.status_code == 200
    cur = r.json()
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")
    assert (await _one(factory, g["uid"])).revoked_at is not None
    await _dead(factory, cur["access_token"])
    assert _err(await _refresh(client, cur["refresh_token"])) == (400, "invalid_grant")
    [audit] = await _audits(factory, "agent_token.revoked")
    assert audit.detail["reason"] == "refresh_reuse"


async def test_f_o5_rotation_loser_revokes(factory, client, monkeypatch):
    """FENCE F-O5. A concurrent winner rotates between our read and our
    UPDATE (simulated). Wrong implementations: SELECT-then-unconditional
    UPDATE (the loser also gets tokens); rowcount 0 ignored (no revoke)."""
    g = await _connected(factory, client)
    rid = (await _one(factory, g["uid"])).id
    _on_entitlements(monkeypatch, factory, update(ApiToken).where(ApiToken.id == rid).values(
        refresh_prev_hash=ApiToken.refresh_hash, refresh_hash="w" * 64))
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")
    row = await _one(factory, g["uid"])
    assert row.revoked_at is not None and row.refresh_hash == "w" * 64


# ── F-O6: audience ─────────────────────────────────────────────────────────


async def test_f_o6_resource_must_be_the_canonical_mcp_url(factory, client):
    """FENCE F-O6. Wrong implementation: ``resource`` ignored anywhere."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    h = await _jwt(factory, uid)
    cid = await _cid(client)
    _, challenge = _pkce()
    bad = "https://evil.example/mcp"
    r = await client.get(CTX, params=_params(cid, challenge, resource=bad), headers=h)
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_target")
    assert _q(r.json()["detail"]["redirect_to"])["error"] == "invalid_target"
    r, _ = await _consent(client, h, cid, resource=bad)
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_target")
    assert await _rows(factory, uid) == []
    r = await client.get(CTX, params=_params(cid, challenge, resource=None), headers=h)
    assert r.status_code == 200 and r.json()["resource"] == RESOURCE
    r, verifier = await _consent(client, h, cid, resource=None)
    assert r.status_code == 200, r.text
    g = {"uid": uid, "cid": cid, "v": verifier, "code": _q(r.json()["redirect_to"])["code"]}
    assert _err(await _exchange(client, g, resource=bad)) == (400, "invalid_target")
    r = await _exchange(client, g, resource=RESOURCE)
    assert r.status_code == 200, r.text
    rt = r.json()["refresh_token"]
    assert _err(await _refresh(client, rt, resource=bad)) == (400, "invalid_target")
    assert (await _refresh(client, rt)).status_code == 200


# ── F-O7: consent endpoints are interactive-only ───────────────────────────


async def test_f_o7_consent_needs_an_interactive_session(factory, client):
    """FENCE F-O7 (the allowlist half lives in test_public_route_allowlist).
    Wrong implementation: consent endpoints public or token-reachable."""
    org = await _org(factory, "A")
    root = await _user(factory, org, "root", superadmin=True)
    plaintext = "pat_" + secrets.token_urlsafe(32)
    from app.services.api_token_service import hash_api_token
    async with factory() as s:
        s.add(ApiToken(token_hash=hash_api_token(plaintext), token_prefix=plaintext[:14],
                       name="rest", scope="write", created_by_user_id=root,
                       created_by_email="r@acme.io", created_at=_naive_now() - timedelta(minutes=5),
                       expires_at=_naive_now() + timedelta(days=5)))
        await s.commit()
    pat = {"Authorization": f"Bearer {plaintext}"}
    assert (await client.get(CTX)).status_code == 401
    assert (await client.post(AUTHZ, json={})).status_code == 401
    assert (await client.get(CTX, headers=pat)).status_code == 403
    assert (await client.post(AUTHZ, json={}, headers=pat)).status_code == 403


# ── F-O9: redirect URI policy ──────────────────────────────────────────────


@pytest.mark.parametrize("uri", [
    "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "http://evil.com/cb",
    "https://claude.ai/cb#frag", "https://user:pw@claude.ai/cb", "http://127.0.0.1.evil.com/cb",
    "http://localhost.evil.com/cb", "https:///cb", "ftp://x.example/cb", "myapp:/cb",
    "https://claude.ai/" + "a" * 512, "https://claude.ai/c b", "https://claude.ai/\u00e9",
])
async def test_f_o9_dcr_refuses_unsafe_redirects(client, uri):
    """FENCE F-O9 (+S13). Wrong implementation: scheme check absent, or a
    loopback host compared by prefix."""
    r = await _register(client, [uri])
    assert _err(r) == (400, "invalid_redirect_uri"), r.text


async def test_f_o9_dcr_accepts_https_loopback_and_private_use(factory, client):
    """FENCE F-O9 (+S2). Wrong implementation: https-only (loopback and
    private-use refused); loopback matched with its port (a Claude Code
    re-authorize from a new ephemeral port fails, a re-registration drains
    the loopback pool)."""
    assert _err(await _register(client, [CB] * 6)) == (400, "invalid_redirect_uri")
    assert _err(await _register(client, [])) == (400, "invalid_redirect_uri")
    good = ["https://claude.ai/cb", "http://127.0.0.1:5555/cb", "http://[::1]/cb",
            "http://localhost/cb", "com.example.app:/cb"]
    r = await _register(client, good)
    assert r.status_code == 201, r.text
    first = await _cid(client, ["http://127.0.0.1:5555/cb"], "Claude Code")
    assert await _cid(client, ["http://127.0.0.1:7000/cb"], "Claude Code") == first
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    _, challenge = _pkce()
    r = await client.get(CTX, params=_params(first, challenge, redirect="http://127.0.0.1:6001/cb"),
                         headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["redirect_uri"] == "http://127.0.0.1:6001/cb"
    r = await client.get(CTX, params=_params(first, challenge, redirect="http://127.0.0.1:6001/other"),
                         headers=h)
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_redirect_uri")
    # The code is bound to the EXACT URI presented at consent, port included.
    r, verifier = await _consent(client, h, first, redirect="http://127.0.0.1:6001/cb")
    assert r.status_code == 200, r.text
    assert r.json()["redirect_to"].startswith("http://127.0.0.1:6001/cb?")
    g = {"cid": first, "v": verifier, "code": _q(r.json()["redirect_to"])["code"]}
    assert _err(await _exchange(client, g, redirect_uri="http://127.0.0.1:5555/cb")) == (
        400, "invalid_grant")
    assert (await _exchange(client, g, redirect_uri="http://127.0.0.1:6001/cb")).status_code == 200


# ── F-O10: no expiry reminders for OAuth rows ──────────────────────────────


async def test_f_o10_sweep_skips_oauth_grants_and_placeholders(factory, client):
    """FENCE F-O10. Wrong implementation: the sweep keyed on ``expires_at``
    over every row (a 1 h access token fires reminders in its first hour)."""
    from app.services.scheduler.jobs.api_token_expiry import (
        FLAG_KEY,
        run_api_token_expiry_reminders,
    )

    g = await _connected(factory, client)
    await _grant(factory, client, uid=g["uid"], cid=g["cid"])  # a placeholder
    from app.services.api_token_service import hash_api_token
    async with factory() as s:
        s.add(SystemSetting(key=FLAG_KEY, value="on"))
        s.add(ApiToken(token_hash=hash_api_token("pat_manual"), token_prefix="pat_manual",
                       name="manual", scope="agent:read", created_by_user_id=g["uid"],
                       created_by_email="m@acme.io",
                       created_at=_naive_now() - timedelta(days=20),
                       expires_at=_naive_now() + timedelta(days=2)))
        await s.commit()
    fired = await run_api_token_expiry_reminders(factory, now=datetime.now(UTC))
    assert fired == 1
    async with factory() as s:
        notes = (await s.execute(select(Notification).where(
            Notification.event_type == "api_token.expiry_reminder"))).scalars().all()
        stages = {r.name: r.reminder_stage for r in (await s.execute(select(ApiToken))).scalars()}
    assert len(notes) == 1 and '"manual"' in notes[0].body
    assert stages == {"manual": 1, "Claude": 0}


# ── F-O11: scope ───────────────────────────────────────────────────────────


async def test_f_o11_scope_parsing_at_consent(factory, client):
    """FENCE F-O11 (+S1). Wrong implementations: ``agent:auto`` grantable;
    a granted scope wider than requested; a space-joined or client-specific
    scope string refused."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    h = await _jwt(factory, uid)
    cid = await _cid(client)
    _, challenge = _pkce()
    for scope, requested in (("agent:read agent:write", "agent:write"), ("agent:write foo:bar", "agent:write"),
                             ("custom", "agent:read"), (None, "agent:read")):
        r = await client.get(CTX, params=_params(cid, challenge, scope=scope), headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["requested_scope"] == requested
    assert r.json()["scopes_offered"] == ["agent:read"]
    for scope in ("agent:auto", "agent:read agent:auto"):
        r = await client.get(CTX, params=_params(cid, challenge, scope=scope), headers=h)
        assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_scope")
        r, _ = await _consent(client, h, cid, scope=scope, granted="agent:read")
        assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_scope")
    for requested, granted in (("agent:write", "agent:auto"), ("agent:read", "agent:write"),
                               ("agent:write", "bogus")):
        r, _ = await _consent(client, h, cid, scope=requested, granted=granted)
        assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_scope"), granted
    assert await _rows(factory, uid) == []
    r, _ = await _consent(client, h, cid, scope="agent:read agent:write", granted="agent:read")
    assert r.status_code == 200, r.text
    assert (await _one(factory, uid)).scope == "agent:read"


async def test_f_o11_refresh_never_widens(factory, client):
    """FENCE F-O11 (R2-2 deviation). Wrong implementation: refresh honouring
    the requested scope (a read grant refreshed into write). A wider KNOWN
    scope is clamped, ``agent:auto`` refused, a narrower one narrows."""
    g = await _connected(factory, client, scope="agent:read")
    r = await _refresh(client, g["refresh_token"], scope="agent:write")
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "agent:read"
    assert (await _one(factory, g["uid"])).scope == "agent:read"
    rt = r.json()["refresh_token"]
    for scope in ("agent:auto", "agent:read agent:auto"):
        assert _err(await _refresh(client, rt, scope=scope)) == (400, "invalid_scope")
    w = await _connected(factory, client, scope="agent:write")
    r = await _refresh(client, w["refresh_token"], scope="agent:read")
    assert r.status_code == 200 and r.json()["scope"] == "agent:read"
    _, row = await _auth(factory, r.json()["access_token"])
    assert row.scope == "agent:read"


# ── F-O12: registration and token-endpoint limits, purge ───────────────────


async def test_f_o12_registration_is_idempotent_and_free(client):
    """FENCE F-O12 (+S3, S5). Wrong implementation: every registration a new
    client / a pool charge, or the per-IP bucket charging idempotent hits
    (150 identical registrations from one IP all succeed)."""
    first = await _register(client)
    assert first.status_code == 201
    body = first.json()
    assert set(body) == {"client_id", "client_id_issued_at", "client_name", "redirect_uris",
                         "grant_types", "response_types", "token_endpoint_auth_method"}
    assert (body["client_name"], body["redirect_uris"], body["grant_types"],
            body["response_types"], body["token_endpoint_auth_method"]) == (
        "Claude", [CB], ["authorization_code", "refresh_token"], ["code"], "none")
    assert isinstance(body["client_id_issued_at"], int) and "client_secret" not in body
    for _ in range(149):
        r = await _register(client, name="Claude", foo="ignored")
        assert r.status_code == 201, r.text
        assert r.json() == body
    assert rate_limit_db.get("oauth:dcr:host:claude.ai") == 1
    assert rate_limit_db.get("oauth:dcr:ip:127.0.0.1") == 1
    # Default name, stripped name.
    assert (await _register(client, name=None)).json()["client_name"] == "MCP client"
    assert (await _register(client, name="  Cursor  ")).json()["client_name"] == "Cursor"
    assert _err(await _register(client, name="x" * 101)) == (400, "invalid_client_metadata")


async def test_f_o12_new_client_pools(client):
    """FENCE F-O12. Wrong implementations: one global pool (a loopback-only
    registration refused once a host's pool is spent); per-IP 10/hour (the
    11th registration from one IP refused)."""
    for i in range(50):
        r = await _register(client, name=f"c{i}", ip="198.51.100.1")
        assert r.status_code == 201, (i, r.text)
    r = await _register(client, name="c50", ip="198.51.100.2")
    assert (r.status_code, r.json()) == (429, {"error": "rate_limited"})
    assert (await _register(client, ["http://localhost/cb"], name="cc", ip="198.51.100.1")
            ).status_code == 201
    for i in range(19):
        r = await _register(client, [f"http://127.0.0.1:{4000 + i}/cb{i}"], name="cc",
                            ip="198.51.100.1")
        assert r.status_code == 201, (i, r.text)
    r = await _register(client, ["http://127.0.0.1/z"], name="cc", ip="198.51.100.1")
    assert _err(r) == (429, "rate_limited")
    assert (await _register(client, ["http://127.0.0.1/z"], name="cc", ip="198.51.100.3")
            ).status_code == 201


async def test_f_o12_new_clients_per_ip_per_hour(client):
    """FENCE F-O12 (S5): 100 new clients per IP per hour, the 101st refused,
    another IP unaffected."""
    for i in range(100):
        r = await _register(client, [f"https://h{i}.example/cb"], ip="203.0.113.7")
        assert r.status_code == 201, (i, r.text)
    assert _err(await _register(client, ["https://h100.example/cb"], ip="203.0.113.7")) == (
        429, "rate_limited")
    assert (await _register(client, ["https://h100.example/cb"], ip="203.0.113.8")
            ).status_code == 201


async def test_f_o12_global_ceiling(factory, client):
    """FENCE F-O12. 20000 live clients refuse a NEW registration (429); an
    idempotent hit still answers."""
    known = await _register(client)
    assert known.status_code == 201
    now = _naive_now()
    async with factory() as s:
        await s.execute(insert(OAuthClient), [
            {"id": f"{i:032x}", "client_name": "x", "redirect_uris": [CB],
             "metadata_key": f"{i:064x}", "created_at": now}
            for i in range(19999)
        ])
        await s.commit()
    assert _err(await _register(client, name="new")) == (429, "rate_limited")
    assert (await _register(client)).json() == known.json()


async def test_f_o12_registration_fails_closed(client, limits_db_down, monkeypatch):
    """FENCE F-O12. Wrong implementation: registering when the limits DB is
    down (the route's slowapi limit off, so the app buckets are reached)."""
    monkeypatch.setattr(limiter, "enabled", False)
    assert _err(await _register(client)) == (503, "temporarily_unavailable")


async def test_f_o12_junk_codes_do_not_lock_out_a_client(factory, client):
    """FENCE F-O12. Wrong implementation: the code limit keyed on
    ``client_id`` (junk codes sent under a hosted client's shared public id
    lock its users out). A found code never consults the failure buckets."""
    g = await _grant(factory, client)
    for i in range(1000):
        r = await _exchange(client, g, code=secrets.token_urlsafe(32),
                            ip=f"10.{i // 250}.{i % 250}.1")
        assert _err(r) == (400, "invalid_grant"), (i, r.text)
    r = await _exchange(client, g, code=secrets.token_urlsafe(32), ip="10.9.9.9")
    assert _err(r) == (429, "rate_limited")
    assert (await _exchange(client, g, ip="10.9.9.9")).status_code == 200


async def test_f_o12_failed_codes_per_ip(factory, client):
    """GUARD: 60 failed codes per IP per minute, then 429; the same IP's valid
    code still works."""
    g = await _grant(factory, client)
    for i in range(60):
        assert _err(await _exchange(client, g, code=f"junk{i}", ip="10.0.0.5")) == (400, "invalid_grant")
    assert _err(await _exchange(client, g, code="junk", ip="10.0.0.5")) == (429, "rate_limited")
    assert (await _exchange(client, g, ip="10.0.0.5")).status_code == 200


async def test_f_o12_purge(factory, client):
    """FENCE F-O12. Wrong implementations: purge on ``created_at`` alone (an
    idle-registered client used yesterday is deleted); clients deleted before
    the expired placeholders (a client whose only row is an expired code
    survives the run); SET NULL on the grant rows."""
    from app.services.scheduler.jobs.oauth_client_purge import run_oauth_client_purge

    g = await _connected(factory, client)  # redeemed grant: client kept
    now = _naive_now()
    old = now - timedelta(days=40)
    async with factory() as s:
        await s.execute(update(OAuthClient).values(created_at=old, last_used_at=old))
        for cid, last in (("a" * 32, None), ("b" * 32, now - timedelta(days=1)),
                          ("c" * 32, None), ("d" * 32, None), ("e" * 32, now - timedelta(days=31))):
            s.add(OAuthClient(id=cid, client_name=cid[0], redirect_uris=[CB],
                              metadata_key=cid * 2, created_at=old, last_used_at=last))
        s.add(OAuthClient(id="f" * 32, client_name="new", redirect_uris=[CB],
                          metadata_key="f" * 64, created_at=now - timedelta(days=2)))
        await s.flush()
        for cid, expires in (("c" * 32, now - timedelta(minutes=1)), ("d" * 32, now + timedelta(seconds=50))):
            s.add(ApiToken(token_hash=cid, token_prefix="oauth_x", name="p", scope="agent:read",
                           created_by_user_id=g["uid"], created_by_email="x@acme.io",
                           created_at=now, expires_at=expires, oauth_client_id=cid,
                           code_hash=cid * 2))
        await s.commit()
    await run_oauth_client_purge(factory, now=now)
    async with factory() as s:
        left = set((await s.execute(select(OAuthClient.id))).scalars())
        placeholders = set((await s.execute(select(ApiToken.oauth_client_id).where(
            ApiToken.refresh_hash.is_(None)))).scalars())
    assert left == {g["cid"], "b" * 32, "d" * 32, "f" * 32}
    assert placeholders == {"d" * 32}
    await _auth(factory, g["access_token"])


async def test_purge_never_raises(factory, monkeypatch):
    """GUARD: a purge failure is logged, never raised (the ticker must not die)."""
    from sqlalchemy.exc import OperationalError

    from app.services.scheduler.jobs.oauth_client_purge import run_oauth_client_purge

    class Broken:
        def __call__(self):
            raise OperationalError("x", {}, Exception("db down"))

    await run_oauth_client_purge(Broken(), now=_naive_now())


# ── F-O14: every advertised URL follows app_url ─────────────────────────────


async def test_f_o14_branch_origin_everywhere(factory, client, monkeypatch):
    """FENCE F-O14. Wrong implementation: a hardcoded production origin."""
    origin = "https://pr-587.branch.example"
    monkeypatch.setattr(settings, "app_url", origin + "/")
    prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm == {"resource": origin + "/mcp", "authorization_servers": [origin],
                   "scopes_supported": ["agent:read", "agent:write"],
                   "bearer_methods_supported": ["header"]}
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert asm == {
        "issuer": origin,
        "authorization_endpoint": origin + "/oauth/authorize",
        "token_endpoint": origin + "/api/v1/oauth/token",
        "registration_endpoint": origin + "/api/v1/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["agent:read", "agent:write"],
        "authorization_response_iss_parameter_supported": True,
    }
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    cid = await _cid(client)
    r, _ = await _consent(client, h, cid, resource=origin + "/mcp")
    assert r.status_code == 200, r.text
    assert _q(r.json()["redirect_to"])["iss"] == origin
    assert www_authenticate() == (
        f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"')


# ── F-O15 / F-O16: live means access OR refresh ────────────────────────────


async def test_f_o15_live_grants_fill_the_cap(factory, client):
    """FENCE F-O15. Wrong implementation: the cap counting ``expires_at``
    only (five grants between refreshes would leave room for more)."""
    g = await _connected(factory, client)
    for _ in range(4):
        r = await _exchange(client, await _grant(factory, client, uid=g["uid"], cid=g["cid"]))
        assert r.status_code == 200
    await _lapse_access(factory, g["uid"])
    r, _ = await _consent(client, g["h"], g["cid"])
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "too_many_agent_tokens")
    r = await client.post("/api/v1/agent/tokens", json=_mint_body(), headers=g["h"])
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "too_many_agent_tokens")


async def test_f_o16_status_is_refresh_aware_and_placeholders_hidden(factory, client):
    """FENCE F-O16 (+S9). Wrong implementation: status keyed on
    ``expires_at`` only (a grant between refreshes lists as expired and
    cannot be downgraded); placeholders listed."""
    g = await _connected(factory, client, role=Role.ADMIN, superadmin=True)
    await _grant(factory, client, uid=g["uid"], cid=g["cid"])  # placeholder
    await _lapse_access(factory, g["uid"])
    for path in ("/api/v1/agent/tokens", "/api/v1/agent/tokens/org", "/api/v1/system/api-tokens"):
        items = (await client.get(path, headers=g["h"])).json()["items"]
        assert [(i["name"], i["status"]) for i in items] == [("Claude", "active")], path
    tid = (await client.get("/api/v1/agent/tokens", headers=g["h"])).json()["items"][0]["id"]
    r = await client.patch(f"/api/v1/agent/tokens/{tid}", json={"scope": "agent:read"},
                           headers=g["h"])
    assert r.status_code == 200, r.text


# ── F-O17: pepper rotation ─────────────────────────────────────────────────


async def test_f_o17_refresh_survives_pepper_rotation(factory, client, monkeypatch):
    """FENCE F-O17. Wrong implementation: refresh lookups by the primary
    hash only (every grant dies at a pepper rotation, and a replayed
    pre-rotation token is no longer recognised as reuse)."""
    old, new = "o" * 64, "n" * 64
    monkeypatch.setattr(settings, "api_token_hmac_key", old)
    g = await _connected(factory, client)
    rotated = (await _refresh(client, g["refresh_token"])).json()
    h = await _connected(factory, client)
    monkeypatch.setattr(settings, "api_token_hmac_key", new)
    monkeypatch.setattr(settings, "api_token_hmac_key_prev", old)
    r = await _refresh(client, h["refresh_token"])
    assert r.status_code == 200, r.text
    assert (await _refresh(client, r.json()["refresh_token"])).status_code == 200
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")
    assert (await _one(factory, g["uid"])).revoked_at is not None
    assert _err(await _refresh(client, rotated["refresh_token"])) == (400, "invalid_grant")


# ── F-O19: entitlement at every step ───────────────────────────────────────


@pytest.mark.parametrize("off", ["feature", "meter"])
async def test_f_o19_entitlement_rechecked_everywhere(factory, client, off):
    """FENCE F-O19. Wrong implementations: the gate on the ``ai.agent`` key
    alone (the meter cell passes); no re-check at the token endpoint."""
    g = await _connected(factory, client)
    g2 = await _grant(factory, client, uid=g["uid"], cid=g["cid"])
    async with factory() as s:
        if off == "feature":
            await s.execute(update(OrgFeatureOverride).where(OrgFeatureOverride.org_id == g["org"])
                            .values(value=False))
        else:
            s.add(OrgLimitOverride(org_id=g["org"], meter="mcp.calls", period="day", limit_value=0))
        await s.commit()
    _, challenge = _pkce()
    assert (await client.get(CTX, params=_params(g["cid"], challenge), headers=g["h"])).status_code == 403
    r, _ = await _consent(client, g["h"], g["cid"])
    assert r.status_code == 403
    assert _err(await _exchange(client, g2)) == (400, "invalid_grant")
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")


# ── F-O20: a concurrent downgrade is not undone ────────────────────────────


async def test_f_o20_downgrade_between_read_and_rotate_survives(factory, client, monkeypatch):
    """FENCE F-O20 (S4, R2-1). Wrong implementations: ``scope`` read at
    SELECT and written back unconditionally (the downgrade is undone); the
    scope miss treated as reuse (the grant revoked)."""
    g = await _connected(factory, client)
    rid = (await _one(factory, g["uid"])).id
    _on_entitlements(monkeypatch, factory, update(ApiToken).where(ApiToken.id == rid)
                     .values(scope="agent:read"))
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")
    row = await _one(factory, g["uid"])
    assert (row.revoked_at, row.scope) == (None, "agent:read")
    r = await _refresh(client, g["refresh_token"])
    assert r.status_code == 200 and r.json()["scope"] == "agent:read"


# ── G1: consent side effects; deny is free ─────────────────────────────────


async def test_g1_consent_side_effects_once_deny_is_free(factory, client):
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    h = await _jwt(factory, uid)
    cid = await _cid(client)
    r, _ = await _consent(client, h, cid, approve=False, password="wrong")
    assert r.status_code == 200, r.text
    q = _q(r.json()["redirect_to"])
    assert q == {"error": "access_denied", "state": "st8", "iss": APP}
    assert r.json()["redirect_to"].startswith(CB + "?")
    assert await _rows(factory, uid) == []
    assert rate_limit_db.get(f"agent:mint:usr:{uid}") == 0
    assert await _audits(factory, "agent_token.created") == []
    r, _ = await _consent(client, h, cid)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    q = _q(r.json()["redirect_to"])
    assert set(q) == {"code", "state", "iss"} and q["state"] == "st8"
    [audit] = await _audits(factory, "agent_token.created")
    assert audit.detail["oauth_client_id"] == cid and audit.detail["redirect_host"] == "claude.ai"
    assert q["code"] not in str(audit.detail)
    async with factory() as s:
        assert len((await s.execute(select(Notification))).scalars().all()) == 1
        assert (await s.get(OAuthClient, cid)).last_used_at is not None
    notification_service.send_notification_email.assert_awaited_once()
    assert rate_limit_db.get(f"agent:mint:usr:{uid}") == 1


async def test_g1_context_does_not_stamp_last_used(factory, client):
    """S10: the context read never keeps a client alive past the purge."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    cid = await _cid(client)
    _, challenge = _pkce()
    r = await client.get(CTX, params=_params(cid, challenge), headers=h)
    assert r.status_code == 200
    assert r.json() == {
        "client_id": cid, "client_name": "Claude", "client_name_verified": False,
        "redirect_uri": CB, "redirect_host": "claude.ai", "requested_scope": "agent:write",
        "scopes_offered": ["agent:read", "agent:write"], "resource": RESOURCE,
    }
    async with factory() as s:
        assert (await s.get(OAuthClient, cid)).last_used_at is None


# ── token endpoint shapes (S7, R2-3, R2-8, S8, S11) ─────────────────────────


async def test_token_endpoint_error_shapes(factory, client):
    g = await _grant(factory, client)
    cases = [
        ({"code": "x"}, "invalid_request"),
        ({"grant_type": "password"}, "unsupported_grant_type"),
        ({"grant_type": "authorization_code", "code_verifier": g["v"]}, "invalid_request"),
        ({"grant_type": "refresh_token"}, "invalid_request"),
    ]
    for data, error in cases:
        r = await client.post(TOKEN, data=data)
        assert _err(r) == (400, error), r.text
        assert r.headers["cache-control"] == "no-store" and g["v"] not in r.text
    r = await client.post(TOKEN, json={"grant_type": "authorization_code", "code": g["code"]})
    assert _err(r) == (400, "invalid_request")
    r = await client.post(TOKEN, files={"grant_type": (None, "authorization_code")})
    assert _err(r) == (400, "invalid_request")
    assert (await _exchange(client, g)).status_code == 200


async def test_deleted_owner_is_invalid_grant(factory, client):
    """S8. Wrong implementation: a NULL owner dereferenced (500)."""
    g = await _connected(factory, client)
    async with factory() as s:
        await s.execute(update(ApiToken).values(created_by_user_id=None))
        await s.commit()
    assert _err(await _refresh(client, g["refresh_token"])) == (400, "invalid_grant")


async def test_expired_oauth_access_is_not_audited(factory, client):
    """S11. An expired OAuth access token is the normal hourly state: same
    401, no ``auth_rejected`` row; a manual token's expiry still audits."""
    g = await _connected(factory, client)
    await _lapse_access(factory, g["uid"])
    await _dead(factory, g["access_token"])
    assert await _audits(factory, "api_token.auth_rejected") == []


async def test_state_must_be_printable_ascii_and_short(factory, client):
    """RFC 6749 state is VSCHAR: a longer or non-ASCII state is refused and
    never echoed (the error redirect still carries ``iss``)."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    cid = await _cid(client)
    _, challenge = _pkce()
    for state in ("été", "s" * 1025):
        r = await client.get(CTX, params=_params(cid, challenge, state=state), headers=h)
        assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_request")
        assert _q(r.json()["detail"]["redirect_to"]) == {"error": "invalid_request", "iss": APP}
    r = await client.get(CTX, params=_params(cid, challenge, state="s" * 1024), headers=h)
    assert r.status_code == 200
