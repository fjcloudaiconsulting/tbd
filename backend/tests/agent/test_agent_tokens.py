"""TBD-578: agent access tokens (``/api/v1/agent/tokens`` and
``app.agent.auth.authenticate_agent_token``).

Routes run on the REAL app with the REAL ``get_current_user`` seam (only
``get_db`` / ``get_session_factory`` point at an in-memory SQLite), so a JWT
stamps ``auth_method="jwt"`` and a ``pat_`` bearer goes through
``authenticate_pat`` exactly as in production.

Fences: F-T1..F-T6, F-A6, M1..M3 and the cutoff-moved recheck (named wrong
implementation in each docstring). Guards: F-S1, M4.
"""
from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app import redis_client
from app.agent.auth import authenticate_agent_token, www_authenticate
from app.agent.registry import AGENT_SCOPES
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.main import app
from app.models import Base
from app.models.api_token import ApiToken
from app.models.audit_event import AuditEvent
from app.models.notification import Notification
from app.models.feature_override import OrgFeatureOverride
from app.models.user import Organization, Role, User
from app.rate_limit import limiter
from app.security import create_access_token, hash_password, token_cutoff
from app.services import api_token_service as svc
from app.services import notification_service
from app.services.api_token_service import hash_api_token

UTC = timezone.utc
PASSWORD = "correct-horse-battery"
BASE = "/api/v1/agent/tokens"


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
    monkeypatch.setattr(
        notification_service, "send_notification_email", AsyncMock(return_value=None)
    )


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


# ── seeding ────────────────────────────────────────────────────────────────


async def _org(factory, name: str, *, agent: bool = True) -> int:
    async with factory() as s:
        org = Organization(name=name, billing_cycle_day=1)
        s.add(org)
        await s.flush()
        if agent:
            s.add(OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True))
        await s.commit()
        return org.id


async def _user(factory, org_id: int, name: str, *, role=Role.MEMBER, superadmin=False) -> int:
    async with factory() as s:
        u = User(
            org_id=org_id, username=name, email=f"{name}@acme.io", first_name=name,
            password_hash=hash_password(PASSWORD), role=role, is_superadmin=superadmin,
            is_active=True, email_verified=True, password_set=True,
        )
        s.add(u)
        await s.commit()
        return u.id


async def _jwt(factory, uid: int) -> dict:
    async with factory() as s:
        u = await s.get(User, uid)
        return {"Authorization": f"Bearer {create_access_token(u.id, u.org_id, u.role.value)}"}


async def _tok(
    factory, owner_id: int | None, scope: str = "agent:write", *,
    created_at: datetime | None = None, revoked: bool = False, expired: bool = False,
) -> str:
    plaintext = "pat_" + secrets.token_urlsafe(32)
    now = _naive_now().replace(microsecond=0)
    async with factory() as s:
        s.add(ApiToken(
            token_hash=hash_api_token(plaintext), token_prefix=plaintext[:14],
            name="harness", scope=scope, created_by_user_id=owner_id,
            created_by_email="x@acme.io",
            created_at=created_at or now - timedelta(minutes=5),
            expires_at=now + (timedelta(days=-1) if expired else timedelta(days=30)),
            revoked_at=now if revoked else None,
        ))
        await s.commit()
    return plaintext


async def _row(factory, plaintext: str) -> ApiToken:
    async with factory() as s:
        return (await s.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_api_token(plaintext))
        )).scalar_one()


def _req() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/mcp", "headers": [],
        "query_string": b"", "client": ("203.0.113.9", 1234),
    })


async def _auth(factory, plaintext: str):
    async with factory() as s:
        return await authenticate_agent_token(_req(), plaintext, s, factory)


def _mint_body(**kw) -> dict:
    return {"name": "claude", "scope": "agent:write", "expires_in_days": 30,
            "current_password": PASSWORD, **kw}


# ── F-T1: REST refuses agent scopes ─────────────────────────────────────────

_probe = APIRouter()


@_probe.get("/probe")
async def _probe_get(u: User = Depends(get_current_user)):
    return {"id": u.id}


@_probe.put("/probe")
async def _probe_put(u: User = Depends(get_current_user)):
    return {"id": u.id}


@pytest.mark.parametrize("scope", ["agent:read", "agent:write", "agent:auto"])
async def test_f_t1_rest_refuses_agent_tokens(factory, scope):
    """FENCE F-T1. Wrong implementations: ``authenticate_pat`` scope check
    relaxed to "starts with read/write" / any scope (superadmin cell goes
    200), or its superadmin check dropped (member cell goes 403/200). The
    probe routes carry no interactive-only guard, so the 403 can only be the
    scope check, and a superadmin REST PAT on the same routes is 200."""
    org = await _org(factory, "A")
    member = await _user(factory, org, "m")
    root = await _user(factory, org, "root", superadmin=True)
    probe = FastAPI()

    async def _db():
        async with factory() as s:
            yield s

    probe.dependency_overrides[get_db] = _db
    probe.dependency_overrides[get_session_factory] = lambda: factory
    probe.include_router(_probe)
    m_tok = await _tok(factory, member, scope)
    r_tok = await _tok(factory, root, scope)
    rest = await _tok(factory, root, "write")
    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://t") as c:
        for method in ("GET", "PUT"):
            r = await c.request(method, "/probe", headers={"Authorization": f"Bearer {m_tok}"})
            assert (r.status_code, r.json()["detail"]) == (401, "Invalid or expired token")
            r = await c.request(method, "/probe", headers={"Authorization": f"Bearer {r_tok}"})
            assert (r.status_code, r.json()["detail"]) == (403, "Token scope insufficient")
            r = await c.request(method, "/probe", headers={"Authorization": f"Bearer {rest}"})
            assert r.status_code == 200, r.text


# ── F-T2: session cutoff, whole seconds, <= ─────────────────────────────────


async def _set_cutoff(factory, uid: int, column: str, at: datetime) -> None:
    async with factory() as s:
        u = await s.get(User, uid)
        setattr(u, column, at)
        await s.commit()


@pytest.mark.parametrize("column", ["sessions_invalidated_at", "password_changed_at"])
async def test_f_t2_cutoff_kills_tokens_at_or_before_it(factory, column):
    """FENCE F-T2. Wrong implementations: no cutoff (all accepted);
    ``sessions_invalidated_at`` only (the password_changed_at cell accepts);
    strict ``<`` (the same-second token survives). Cutoff written at whole
    seconds, as MySQL stores it."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    t = _naive_now().replace(microsecond=0) - timedelta(hours=1)
    before = await _tok(factory, uid, created_at=t - timedelta(seconds=1))
    same = await _tok(factory, uid, created_at=t)
    after = await _tok(factory, uid, created_at=t + timedelta(seconds=1))
    await _set_cutoff(factory, uid, column, t)
    for dead in (before, same):
        with pytest.raises(HTTPException) as ei:
            await _auth(factory, dead)
        assert ei.value.status_code == 401
    user, row = await _auth(factory, after)
    assert user.id == uid and row.scope == "agent:write"


async def test_f_t2_mint_stamps_created_at_whole_seconds(factory):
    """FENCE F-T2. Wrong implementation: ``created_at`` stamped app-side
    without the floor (SQLite would keep microseconds, so a same-second
    token would sort after a whole-second cutoff and survive)."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    async with factory() as s:
        user = await s.get(User, uid)
        _, row = await svc.mint(s, user=user, name="n", scope="agent:read", expires_in_days=1)
    assert row.created_at.microsecond == 0


# ── F-T3: one generic 401 ───────────────────────────────────────────────────


async def test_f_t3_every_rejection_is_the_same_401(factory):
    structlog.contextvars.clear_contextvars()
    """FENCE F-T3. Wrong implementations: per-reason bodies (an oracle); the
    owner's ``is_active`` not re-read (inactive cell authenticates); a REST
    PAT accepted as an agent credential (rest cell authenticates)."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    gone = await _user(factory, org, "gone")
    t = _naive_now().replace(microsecond=0) - timedelta(hours=1)
    cases = {
        "unknown": "pat_" + secrets.token_urlsafe(32),
        "not_pat": "eyJhbGciOi.jwt.like",
        "revoked": await _tok(factory, uid, revoked=True),
        "expired": await _tok(factory, uid, expired=True),
        "owner_inactive": await _tok(factory, gone),
        "owner_null": await _tok(factory, None),
        "rest_scope": await _tok(factory, uid, "write"),
        "before_cutoff": await _tok(factory, uid, created_at=t),
    }
    ok = await _tok(factory, uid, created_at=t + timedelta(minutes=10))
    await _set_cutoff(factory, uid, "sessions_invalidated_at", t + timedelta(seconds=30))
    async with factory() as s:
        (await s.get(User, gone)).is_active = False
        await s.commit()
    seen = set()
    for reason, plaintext in cases.items():
        with pytest.raises(HTTPException) as ei:
            await _auth(factory, plaintext)
        e = ei.value
        seen.add((e.status_code, e.detail, tuple(sorted((e.headers or {}).items()))))
    # TBD-561 (F-O8): one header for every reason, pointing at the metadata.
    assert seen == {(401, "Invalid or expired token", (("WWW-Authenticate", www_authenticate()),))}
    assert "resource_metadata=" in www_authenticate()
    # Known-but-dead tokens are audited with the token in the ACTOR column:
    # the bind precedes the rejection branches (the pat.py rule).
    rejected = await _audits(factory, "api_token.auth_rejected")
    assert sorted(a.detail["reason"] for a in rejected) == ["expired", "revoked"]
    assert all(a.api_token_id == a.detail["api_token_id"] for a in rejected)
    user, _ = await _auth(factory, ok)
    assert user.id == uid


# ── F-T4: scope families never cross ────────────────────────────────────────


@pytest.mark.parametrize("scope", ["read", "write"])
async def test_f_t4_member_cannot_mint_rest_scopes(factory, client, scope):
    """FENCE F-T4. Wrong implementation: the agent mint schema accepting REST
    scopes (the member would get a superadmin-shaped ``read``/``write``)."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    h = await _jwt(factory, uid)
    r = await client.post(BASE, json=_mint_body(scope=scope), headers=h)
    assert r.status_code == 422, r.text
    r = await client.post(
        "/api/v1/system/api-tokens",
        json={"name": "x", "scope": scope, "expires_in_days": 30, "current_password": PASSWORD},
        headers=h,
    )
    assert r.status_code == 403, r.text


# ── F-T5: the agent surface never touches a REST PAT ────────────────────────


async def test_f_t5_agent_routes_cannot_touch_a_rest_pat(factory, client):
    """FENCE F-T5. Wrong implementation: revoke / PATCH / revoke-all not
    filtering on scope, so a superadmin's REST PAT dies (or changes) here."""
    org = await _org(factory, "A")
    root = await _user(factory, org, "root", superadmin=True)
    h = await _jwt(factory, root)
    rest = await _tok(factory, root, "write")
    rid = (await _row(factory, rest)).id
    r = await client.delete(f"{BASE}/{rid}", headers=h)
    assert r.status_code == 404, r.text
    r = await client.patch(f"{BASE}/{rid}", json={"scope": "agent:read"}, headers=h)
    assert r.status_code == 404, r.text
    r = await client.post(f"{BASE}/revoke-all", headers=h)
    assert r.status_code == 200 and r.json()["revoked"] == 0
    r = await client.get(BASE, headers=h)
    assert r.json()["items"] == []
    row = await _row(factory, rest)
    assert row.revoked_at is None and row.scope == "write"


# ── F-T6: org admin stays in their org ──────────────────────────────────────


async def test_f_t6_org_admin_is_bounded_by_org(factory, client):
    """FENCE F-T6. Wrong implementation: the org-admin query keyed on the
    token id only (org B's token listed and revocable)."""
    a = await _org(factory, "A")
    b = await _org(factory, "B")
    admin = await _user(factory, a, "adm", role=Role.ADMIN)
    member = await _user(factory, a, "m")
    other = await _user(factory, b, "o")
    mine = await _tok(factory, member)
    theirs = await _tok(factory, other)
    # The member signed out everywhere after minting: the org view must judge
    # the token by the OWNER's cutoff, not the admin's.
    await _set_cutoff(factory, member, "sessions_invalidated_at", _naive_now().replace(microsecond=0))
    h = await _jwt(factory, admin)
    r = await client.get(f"{BASE}/org", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["owner_user_id"], i["status"]) for i in items] == [(member, "invalidated")]
    tid = (await _row(factory, theirs)).id
    r = await client.delete(f"{BASE}/org/{tid}", headers=h)
    assert r.status_code == 404
    assert (await _row(factory, theirs)).revoked_at is None
    mid = (await _row(factory, mine)).id
    r = await client.delete(f"{BASE}/org/{mid}", headers=h)
    assert r.status_code == 200
    assert (await _row(factory, mine)).revoked_at is not None
    [audit] = await _audits(factory, "agent_token.revoked")
    assert (audit.actor_user_id, audit.target_org_id) == (admin, a)
    assert (audit.detail["by_admin"], audit.detail["owner_user_id"]) == (True, member)
    r = await client.get(f"{BASE}/org", headers=await _jwt(factory, member))
    assert r.status_code == 403


# ── F-A6: auto rules and downward-only PATCH ───────────────────────────────


async def test_f_a6_auto_mint_rules(factory, client):
    """FENCE F-A6 (mint half). Wrong implementations: auto mintable without
    the acknowledgment, or for more than 30 days. Also M4: the 422 body
    never echoes the step-up proofs."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    r = await client.post(BASE, json=_mint_body(
        scope="agent:auto", stepup_token="proof-abc", mfa_code="123456"), headers=h)
    assert r.status_code == 422
    assert "proof-abc" not in r.text and "123456" not in r.text and PASSWORD not in r.text
    r = await client.post(BASE, json=_mint_body(
        scope="agent:auto", acknowledge_auto=True, expires_in_days=31), headers=h)
    assert r.status_code == 422
    r = await client.post(BASE, json=_mint_body(
        scope="agent:auto", acknowledge_auto=True, expires_in_days=30), headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["scope"] == "agent:auto"


@pytest.mark.parametrize("start,target", [
    ("agent:write", "agent:auto"), ("agent:read", "agent:write"),
    ("agent:read", "agent:auto"), ("agent:auto", "agent:auto"),
])
async def test_f_a6_patch_never_goes_up(factory, client, start, target):
    """FENCE F-A6. Wrong implementation: upgrade in place (any scope
    accepted, or ``>`` instead of ``>=`` letting a no-op through)."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    tok = await _tok(factory, uid, start)
    tid = (await _row(factory, tok)).id
    r = await client.patch(f"{BASE}/{tid}", json={"scope": target}, headers=await _jwt(factory, uid))
    assert (r.status_code, r.json()["detail"]["code"]) == (422, "scope_not_downward")
    assert (await _row(factory, tok)).scope == start


@pytest.mark.parametrize("dead", ["revoked", "expired"])
async def test_patch_on_a_dead_token_is_404(factory, client, dead):
    """FENCE. Wrong implementation: PATCH without the live-status check."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    tok = await _tok(factory, uid, "agent:auto", **{dead: True})
    tid = (await _row(factory, tok)).id
    r = await client.patch(f"{BASE}/{tid}", json={"scope": "agent:read"}, headers=await _jwt(factory, uid))
    assert r.status_code == 404
    assert (await _row(factory, tok)).scope == "agent:auto"


async def test_f_a6_downgrade_is_effective_on_the_next_call(factory, client):
    """FENCE F-A6. Wrong implementation: the downgrade not persisted, or the
    authenticator caching the scope."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    tok = await _tok(factory, uid, "agent:auto")
    tid = (await _row(factory, tok)).id
    r = await client.patch(f"{BASE}/{tid}", json={"scope": "agent:write"}, headers=await _jwt(factory, uid))
    assert r.status_code == 200 and r.json()["scope"] == "agent:write"
    _, row = await _auth(factory, tok)
    assert row.scope == "agent:write"
    audits = await _audits(factory, "agent_token.downgraded")
    assert [(a.detail["from"], a.detail["to"]) for a in audits] == [("agent:auto", "agent:write")]


# ── M1: at most 5 live ──────────────────────────────────────────────────────


async def test_m1_five_live_tokens_max_dead_ones_do_not_count(factory, client):
    """FENCE M1. Wrong implementations: no cap; counting revoked, expired or
    cutoff-invalidated rows (the 4 live + 3 dead fixture would 409 early)."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    t = _naive_now().replace(microsecond=0) - timedelta(hours=1)
    await _tok(factory, uid, revoked=True)
    await _tok(factory, uid, expired=True)
    await _tok(factory, uid, created_at=t)  # killed by the cutoff below
    for _ in range(4):
        await _tok(factory, uid, created_at=t + timedelta(minutes=10))
    await _tok(factory, uid, "write")  # a REST-scoped row never counts
    await _set_cutoff(factory, uid, "sessions_invalidated_at", t + timedelta(seconds=30))
    h = await _jwt(factory, uid)
    r = await client.post(BASE, json=_mint_body(), headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(BASE, json=_mint_body(), headers=h)
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "too_many_agent_tokens")
    statuses = sorted(i["status"] for i in (await client.get(BASE, headers=h)).json()["items"])
    assert statuses == ["active"] * 5 + ["expired", "invalidated", "revoked"]


async def test_m1_mint_refuses_when_the_cutoff_moved_mid_request(factory):
    """FENCE (sign-off fold 2). Wrong implementation: no re-read of the
    cutoff under the owner-row lock, so a token minted while the owner signed
    out everywhere survives it."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    async with factory() as s:
        user = await s.get(User, uid)
        seen = token_cutoff(user)
        await _set_cutoff(factory, uid, "sessions_invalidated_at", _naive_now().replace(microsecond=0))
        with pytest.raises(svc.SessionCutoffMoved):
            await svc.mint_agent(s, user=user, name="n", scope="agent:read",
                                 expires_in_days=1, cutoff_seen=seen)
    async with factory() as s:
        assert (await s.execute(select(ApiToken))).scalars().all() == []


# ── M2: only mint needs ai.agent ────────────────────────────────────────────


async def test_m2_only_mint_is_gated_on_ai_agent(factory, client):
    """FENCE M2. Wrong implementations: mint ungated (201), or the gate on
    the whole router (a user whose org lost ``ai.agent`` cannot revoke)."""
    org = await _org(factory, "A", agent=False)
    uid = await _user(factory, org, "m")
    tok = await _tok(factory, uid)
    h = await _jwt(factory, uid)
    r = await client.post(BASE, json=_mint_body(), headers=h)
    assert (r.status_code, r.json()["detail"]["code"]) == (403, "feature_not_enabled")
    assert len((await client.get(BASE, headers=h)).json()["items"]) == 1
    tid = (await _row(factory, tok)).id
    assert (await client.delete(f"{BASE}/{tid}", headers=h)).status_code == 200


async def test_f_e4_mint_refused_when_mcp_calls_limit_is_zero(factory, client):
    """FENCE F-E4 (mint half, TBD-585). ``ai.agent`` on but the effective
    ``mcp.calls`` limit 0 (here a per-org limit override): mint is 403, before
    the step-up and the per-user bucket. A limit of 1 still mints. Wrong
    implementation: gating mint on the ``ai.agent`` key alone (201)."""
    from app.models.limit_override import OrgLimitOverride

    org = await _org(factory, "Z")
    async with factory() as s:
        s.add(OrgLimitOverride(org_id=org, meter="mcp.calls", period="day", limit_value=0))
        await s.commit()
    uid = await _user(factory, org, "z")
    h = await _jwt(factory, uid)
    r = await client.post(BASE, json=_mint_body(), headers=h)
    assert r.status_code == 403
    assert r.json()["detail"] == {
        "code": "feature_not_enabled", "feature_key": "ai.agent", "meter": "mcp.calls",
    }
    async with factory() as s:
        row = (await s.execute(
            select(OrgLimitOverride).where(OrgLimitOverride.org_id == org)
        )).scalar_one()
        row.limit_value = 1
        await s.commit()
    assert (await client.post(BASE, json=_mint_body(), headers=h)).status_code == 201


# ── M3: per-user mint bucket ────────────────────────────────────────────────


async def test_m3_per_user_bucket_counts_failed_step_ups(factory, client, monkeypatch):
    """FENCE M3. Wrong implementations: the bucket after the step-up (failed
    proofs never count); a bucket keyed on the client IP (each call below
    comes from a new IP, as a grinder rotating IPs would); one global key
    (the second user would be refused too). Every call has its own IP, so
    the slowapi IP limit can never produce the 429."""
    monkeypatch.setenv("PFV_RUNTIME", "app_platform")  # trust do-connecting-ip
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    other = await _jwt(factory, await _user(factory, org, "o"))
    for i in range(10):
        r = await client.post(BASE, json=_mint_body(current_password="wrong"),
                              headers={**h, "do-connecting-ip": f"198.51.100.{i}"})
        assert r.status_code == 401, r.text
    r = await client.post(BASE, json=_mint_body(), headers={**h, "do-connecting-ip": "198.51.100.99"})
    assert (r.status_code, r.json()["detail"]["code"]) == (429, "mint_rate_limited")
    r = await client.post(BASE, json=_mint_body(), headers={**other, "do-connecting-ip": "198.51.100.98"})
    assert r.status_code == 201, r.text


async def test_m3_bucket_fails_closed(factory, client, monkeypatch):
    """FENCE M3. Wrong implementation: minting when Redis is unavailable."""
    org = await _org(factory, "A")
    h = await _jwt(factory, await _user(factory, org, "m"))
    monkeypatch.setattr(redis_client, "get_client", lambda: None)
    r = await client.post(BASE, json=_mint_body(), headers=h)
    assert (r.status_code, r.json()["detail"]["code"]) == (503, "limits_unavailable")
    async with factory() as s:
        assert (await s.execute(select(ApiToken))).scalars().all() == []


# ── M4 guards: step-up, reveal-once, audit, notification copy ──────────────


async def _audits(factory, event_type: str) -> list[AuditEvent]:
    async with factory() as s:
        return list((await s.execute(
            select(AuditEvent).where(AuditEvent.event_type == event_type)
        )).scalars().all())


async def test_m4_mint_success_audit_and_auto_copy(factory, client):
    """GUARD M4. Wrong password -> 401 + failure audit; success is
    reveal-once under no-store; neither audit nor notification carries the
    plaintext; the auto token's notification says auto-mode."""
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    h = await _jwt(factory, uid)
    r = await client.post(BASE, json=_mint_body(current_password="nope"), headers=h)
    assert r.status_code == 401
    r = await client.post(BASE, json=_mint_body(
        scope="agent:auto", acknowledge_auto=True), headers=h)
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    token = r.json()["token"]
    assert token.startswith("pat_")
    audits = await _audits(factory, "agent_token.created")
    assert sorted(a.outcome.value if hasattr(a.outcome, "value") else a.outcome
                  for a in audits) == ["failure", "success"]
    assert all(token not in str(a.detail) for a in audits)
    assert all(a.target_org_id == org for a in audits)
    async with factory() as s:
        n = (await s.execute(select(Notification))).scalars().one()
    assert "auto-mode" in n.title.lower() and token not in n.body
    assert n.link_url == "/settings/agent-tokens"
    email = notification_service.send_notification_email
    email.assert_awaited_once()
    assert "auto-mode" in email.await_args.kwargs["title"].lower()
    assert email.await_args.kwargs["link_url"] == "/settings/agent-tokens"
    assert token not in str(email.await_args)
    user, row = await _auth(factory, token)
    assert (user.id, row.scope) == (uid, "agent:auto")


async def test_m4_list_and_revoke_all_are_own_agent_tokens_only(factory, client):
    org = await _org(factory, "A")
    uid = await _user(factory, org, "m")
    other = await _user(factory, org, "o")
    await _tok(factory, uid, "agent:read")
    await _tok(factory, uid, "agent:auto")
    theirs = await _tok(factory, other)
    h = await _jwt(factory, uid)
    items = (await client.get(BASE, headers=h)).json()["items"]
    assert sorted(i["scope"] for i in items) == ["agent:auto", "agent:read"]
    assert "token" not in items[0] and "token_hash" not in items[0]
    r = await client.post(f"{BASE}/revoke-all", headers=h)
    assert r.json()["revoked"] == 2
    assert (await _row(factory, theirs)).revoked_at is None
    oid = (await _row(factory, theirs)).id
    assert (await client.delete(f"{BASE}/{oid}", headers=h)).status_code == 404


def test_scope_rank_matches_registry():
    assert set(svc.AGENT_SCOPE_RANK) == set(AGENT_SCOPES)
