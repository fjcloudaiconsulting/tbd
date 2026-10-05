"""TBD-561: the MCP server component (``app.mcp_main``).

Driven over HTTP through the REAL ``mcp_main.app`` (httpx ASGITransport) on a
file-backed SQLite and the suite's fake Redis, so every request takes the path
production takes: IP limit, agent-token auth, the entitlement door, JSON-RPC
dispatch, then ``registry.invoke`` with its gates and meter admission.

Fences: F-M1, F-M2, F-M4 (+F-O8), F-E4 (door), F-R8, F-Q5, F-Q12, gate-6 fail
mode, F-A1, annotations. Guards: protocol handling. Each fence names the wrong
implementation it kills.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import mcp_main, rate_limit_db
from app._time import utcnow_naive
from app.agent import registry
from app.agent.registry import Change, Preview, ToolSpec
from app.models import Category, Organization
from app.models.agent_pending_action import AgentPendingAction
from app.models.api_token import ApiToken
from app.models.base import Base
from app.models.billing import BillingPeriod
from app.models.budget import Budget
from app.models.category import CategoryType
from app.models.feature_override import OrgFeatureOverride
from app.models.limit_override import OrgLimitOverride
from app.models.usage_counter import UsageCounter
from app.models.user import Role, User
from app.config import settings
from app.security import hash_password
from app.services.api_token_service import hash_api_token
from app.services.feature_gate import Feature

P_START = utcnow_naive().date().replace(day=1)
METHODS = ["initialize", "notifications/initialized", "ping", "tools/list", "tools/call"]


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def factory(tmp_path, monkeypatch):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/m.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(mcp_main, "session_factory", f)
    try:
        yield f
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def client(factory):
    async with AsyncClient(transport=ASGITransport(app=mcp_main.app), base_url="http://t") as c:
        yield c


def _tok(name: str) -> str:
    return f"pat_test_{name}_" + "x" * 20


@pytest_asyncio.fixture
async def w(factory):
    """One org with ``ai.agent`` on, a budget, and one token per scope plus a
    REST PAT and a revoked agent token."""
    async with factory() as db:
        org = Organization(name="Org", billing_cycle_day=1, primary_currency="EUR")
        db.add(org)
        await db.flush()
        user = User(
            org_id=org.id, username="m", email="m@x.example",
            password_hash=hash_password("pw-1234567"), role=Role.MEMBER, is_active=True,
            email_verified=True,
        )
        cat = Category(org_id=org.id, name="Food", type=CategoryType.EXPENSE)
        db.add_all([user, cat])
        await db.flush()
        budget = Budget(org_id=org.id, category_id=cat.id, amount=Decimal("100.00"),
                        period_start=P_START)
        # Created a minute ago: the cutoff check is ``created_at <= cutoff``.
        made = datetime.datetime.utcnow().replace(microsecond=0) - datetime.timedelta(minutes=1)
        exp = made + datetime.timedelta(days=30)
        toks = {}
        for name, scope, revoked in [
            ("read", "agent:read", None), ("write", "agent:write", None),
            ("write2", "agent:write", None), ("auto", "agent:auto", None),
            ("rest", "read", None), ("revoked", "agent:write", made),
        ]:
            t = ApiToken(
                token_hash=hash_api_token(_tok(name)), token_prefix=_tok(name)[:14], name=name,
                scope=scope, created_by_user_id=user.id, created_by_email="m@x.example",
                created_at=made, expires_at=exp, revoked_at=revoked,
            )
            db.add(t)
            toks[name] = t
        db.add_all([
            BillingPeriod(org_id=org.id, start_date=P_START), budget,
            OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True),
        ])
        await db.commit()
        return {"org": org.id, "user": user.id, "budget": budget.id,
                "tok": {k: v.id for k, v in toks.items()}}


@pytest.fixture
def scratch():
    added: list[str] = []
    calls: list[str] = []

    class _Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        budget_id: int

    def _add(name, *, risk):
        async def _preview(ctx, args):
            return Preview(summary="scratch", changes=[
                Change("budgets", args.budget_id, "amount", "1.00", "2.00", "EUR")])

        async def _execute(ctx, args):
            calls.append(name)
            return {"ok": True}

        registry.register(ToolSpec(
            name=name, risk=risk, args=_Args, product_area=Feature.BUDGETS, min_role=Role.MEMBER,
            mirrors_route=("PUT", "/api/v1/budgets/{budget_id}"), description="t",
            preview=_preview, execute=_execute,
        ))
        added.append(name)
        return calls

    yield _add
    for n in added:
        registry._TOOLS.pop(n, None)


# ── helpers ───────────────────────────────────────────────────────────────

_ids = iter(range(1, 10**6))


def _msg(method: str, params: dict | None = None) -> dict:
    m: dict = {"jsonrpc": "2.0", "method": method}
    if not method.startswith("notifications/"):
        m["id"] = next(_ids)
    if params is not None:
        m["params"] = params
    elif method == "initialize":
        m["params"] = {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}
    elif method == "tools/call":
        m["params"] = {"name": "accounts_list", "arguments": {}}
    return m


async def _post(client, body, token: str | None = "write", ip: str = "203.0.113.7", raw=None):
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if token is not None:
        headers["authorization"] = f"Bearer {_tok(token) if token in _TOKENS else token}"
    # The fake transport's peer is 127.0.0.1 (a trusted proxy), so the IP
    # limiter reads this header, as it would behind nginx.
    headers["x-forwarded-for"] = ip
    content = raw if raw is not None else json.dumps(body)
    return await client.post("/mcp", content=content, headers=headers)


_TOKENS = {"read", "write", "write2", "auto", "rest", "revoked"}


async def _call(client, name, args, token="write", **kw):
    return await _post(client, _msg("tools/call", {"name": name, "arguments": args}), token, **kw)


async def _count(f, w) -> int:
    async with f() as db:
        return int(await db.scalar(
            select(UsageCounter.value).where(UsageCounter.org_id == w["org"],
                                             UsageCounter.meter == "mcp.calls")
        ) or 0)


def _tools(resp) -> dict[str, dict]:
    return {t["name"]: t for t in resp.json()["result"]["tools"]}


# ── F-M1: route set and import closure ───────────────────────────────────

def test_fm1_route_set_is_exactly_post_mcp_and_get_health():
    """FENCE F-M1. Wrong implementation: a REST router (or an SDK sub-app
    answering every method under /mcp) mounted on the MCP component."""
    pairs = set()
    for r in mcp_main.app.routes:
        for m in getattr(r, "methods", None) or {"*"}:
            if m != "HEAD":
                pairs.add((m, r.path))
    assert pairs == {("POST", "/mcp"), ("GET", "/health")}


def test_fm1_mcp_main_does_not_import_app_main():
    """FENCE F-M1. Wrong implementation: ``mcp_main`` importing ``app.main``
    (the app that mounts the REST routers, the scheduler and the migration
    guard). Tools import two router MODULES for their handler functions;
    nothing mounts them, which the route-set fence above proves."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, app.mcp_main; print('app.main' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.split()[-1] == "False", out.stdout


async def test_health_is_open(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ── F-M4 + F-O8: auth before every method ────────────────────────────────

@pytest.mark.parametrize("token", [None, "pat_garbage_not_a_token", "rest", "revoked", "Bearer"])
@pytest.mark.parametrize("method", METHODS + ["<malformed>"])
async def test_fm4_every_method_401s_without_a_valid_agent_bearer(client, w, factory, token, method):
    """FENCE F-M4 + F-O8. Wrong implementations: auth applied only inside
    tools/call (initialize/ping answer anonymously), a malformed body parsed
    and answered before auth, a REST PAT accepted, and a bare
    ``WWW-Authenticate: Bearer`` with no ``resource_metadata``."""
    raw = "{not json" if method == "<malformed>" else None
    body = None if raw else _msg(method)
    r = await _post(client, body, token=token, raw=raw)
    assert r.status_code == 401
    origin = settings.app_url.rstrip("/")
    assert r.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"'
    )
    assert r.json() == {"detail": "Invalid or expired token"}
    assert await _count(factory, w) == 0


# ── F-E4: the entitlement door on every method ───────────────────────────

@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("off", ["feature", "meter"])
async def test_fe4_door_refuses_every_method(client, w, factory, method, off):
    """FENCE F-E4. Wrong implementations: gating on the ``ai.agent`` key
    alone (a 0 ``mcp.calls`` limit still serves), or checking only inside
    tools/call (initialize, ping and tools/list still answer)."""
    async with factory() as db:
        if off == "feature":
            row = await db.scalar(select(OrgFeatureOverride).where(
                OrgFeatureOverride.org_id == w["org"]))
            row.value = False
        else:
            db.add(OrgLimitOverride(org_id=w["org"], meter="mcp.calls", period="month",
                                    limit_value=0))
        await db.commit()
    r = await _post(client, _msg(method))
    assert r.status_code == 403
    assert "www-authenticate" not in r.headers
    # Nothing is parsed before the door: a malformed body is refused the same.
    assert (await _post(client, None, raw="{not json")).status_code == 403
    err = r.json()["error"]["data"]
    assert err == {"code": "feature_not_enabled", "feature_key": "ai.agent", "meter": "mcp.calls"}
    assert await _count(factory, w) == 0


# ── F-R8: scope filters list AND call ────────────────────────────────────

async def test_fr8_read_token_sees_and_calls_only_read_tools(client, w, factory):
    """FENCE F-R8. Wrong implementation: the scope check applied on
    tools/list only, so an ``agent:read`` token can still call a write tool
    or confirm an action by name."""
    listed = _tools(await _post(client, _msg("tools/list"), token="read"))
    risks = {n: registry.get_tool(n) for n in listed}
    assert listed and all(s is not None and s.risk == "read" for s in risks.values())
    assert "confirm_action" not in listed and "cancel_action" not in listed

    r = await _call(client, "budgets_update_amount",
                    {"budget_id": w["budget"], "amount": "120.00"}, token="read")
    res = r.json()["result"]
    assert r.status_code == 200 and res["isError"] is True
    assert res["structuredContent"]["code"] == "scope_denied"
    r = await _call(client, "confirm_action", {"action_id": "0" * 32}, token="read")
    assert r.json()["result"]["structuredContent"]["code"] == "scope_denied"
    r = await _call(client, "cancel_action", {"action_id": "0" * 32}, token="read")
    assert r.json()["result"]["structuredContent"]["code"] == "scope_denied"
    # Neither decision tool spent the meter (refused before gate 6 / admission);
    # the write call above did (refused at gate 5, after admission, by design).
    assert await _count(factory, w) == 1


async def test_write_token_lists_writes_and_the_decision_tools(client, w):
    listed = _tools(await _post(client, _msg("tools/list"), token="write"))
    assert "budgets_update_amount" in listed and "confirm_action" in listed
    assert "cancel_action" in listed
    assert set(listed) - {"confirm_action", "cancel_action"} == {
        t.name for t in registry.all_tools()}


# ── annotations per risk class and token ─────────────────────────────────

async def test_annotations_follow_risk_and_token(client, w, scratch):
    """FENCE (A1.2). Wrong implementation: annotations from the risk class
    alone, so a write tool reads non-destructive on an ``agent:auto`` token
    that executes it, or ``readOnlyHint`` on a write."""
    scratch("sens_tool", risk="sensitive")
    for token, write_destructive in [("write", False), ("auto", True)]:
        listed = _tools(await _post(client, _msg("tools/list"), token=token))
        assert listed["accounts_list"]["annotations"] == {"readOnlyHint": True}
        assert listed["budgets_update_amount"]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": write_destructive}
        assert listed["sens_tool"]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": True}
        assert listed["confirm_action"]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": True}
        assert listed["cancel_action"]["annotations"] == {
            "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
    schema = listed["budgets_update_amount"]["inputSchema"]
    assert schema["type"] == "object" and "budget_id" in schema["properties"]
    assert schema.get("additionalProperties") is False


# ── F-Q5: only tools/call moves the meter ────────────────────────────────

async def test_fq5_only_tools_call_moves_mcp_calls(client, w, factory):
    """FENCE F-Q5 (transport half). Wrong implementation: counting at the
    transport per JSON-RPC message, so the handshake and listing burn the
    org's plan meter."""
    for m in ["initialize", "notifications/initialized", "ping", "tools/list"]:
        r = await _post(client, _msg(m))
        assert r.status_code in (200, 202), (m, r.text)
    assert await _count(factory, w) == 0
    r = await _call(client, "accounts_list", {})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False
    assert await _count(factory, w) == 1


# ── gate 6: per-token limit above admission ──────────────────────────────

@pytest.fixture
def frozen(monkeypatch):
    """Pin the fixed-window clock so a pre-filled bucket cannot roll over
    between the fill and the call."""
    import time as _time

    now = _time.time()
    monkeypatch.setattr(rate_limit_db, "_clock", lambda: now)


def _exhaust_minute(token_id: int) -> None:
    rate_limit_db.hit(f"agent:tok:{token_id}:calls:min", 60, amount=120)


async def test_fm2_limits_are_keyed_on_the_token_not_the_ip(client, w, factory, frozen):
    """FENCE F-M2. Wrong implementation: the per-call limit keyed on the
    client IP, so one harness exhausting it locks out every token behind the
    same NAT (or one token rotates IPs to escape it)."""
    _exhaust_minute(w["tok"]["write"])
    a = await _call(client, "accounts_list", {}, token="write")
    assert a.status_code == 429 and a.headers["retry-after"] == "60"
    assert a.json()["error"]["data"]["code"] == "token_rate_limited"
    b = await _call(client, "accounts_list", {}, token="write2")
    assert b.status_code == 200 and b.json()["result"]["isError"] is False


async def test_one_call_draws_exactly_one_from_the_bucket(client, w, frozen):
    """FENCE. Wrong implementation: the front door also charging tools/call
    (the registry charges it), halving every token's real limit."""
    assert (await _call(client, "accounts_list", {})).status_code == 200
    assert rate_limit_db.get(f"agent:tok:{w['tok']['write']}:calls:min") == 1
    assert rate_limit_db.get(f"agent:tok:{w['tok']['write']}:req:min") == 1


async def test_fq12_a_call_refused_at_gate_6_does_not_burn_the_meter(
    client, w, factory, frozen
):
    """FENCE F-Q12 (rate-limit half). Wrong implementation: ``mcp.calls``
    admitted before the per-token limit, so a throttled harness still drains
    the org's monthly meter."""
    _exhaust_minute(w["tok"]["write"])
    for name, args in [("accounts_list", {}),
                       ("budgets_update_amount", {"budget_id": w["budget"], "amount": "1.00"}),
                       ("confirm_action", {"action_id": "0" * 32}),
                       ("cancel_action", {"action_id": "0" * 32})]:
        r = await _call(client, name, args, token="write")
        assert r.status_code == 429, (name, r.text)
    assert await _count(factory, w) == 0


async def test_gate_6_daily_bucket(client, w, factory, frozen):
    rate_limit_db.hit(f"agent:tok:{w['tok']['write']}:calls:day", 86_400, amount=2000)
    r = await _call(client, "accounts_list", {}, token="write")
    assert r.status_code == 429
    assert await _count(factory, w) == 0


async def test_a2_limits_down_is_503_for_every_risk(client, w, factory, limits_hit_down):
    """A2. Wrong implementation: the request bucket (or gate 6) failing open,
    so calls are admitted and spend the meter while limits are unavailable."""
    for name, args in [("budgets_update_amount", {"budget_id": w["budget"], "amount": "1.00"}),
                       ("confirm_action", {"action_id": "0" * 32}),
                       ("accounts_list", {})]:
        r = await _call(client, name, args, token="write")
        assert r.status_code == 503, (name, r.text)
        assert r.json()["error"]["code"] == mcp_main.UNAVAILABLE
    assert await _count(factory, w) == 0
    # A method with no gate 6 behind it: only the request bucket can refuse.
    r = await _post(client, _msg("ping"), token="write")
    assert r.status_code == 503 and r.json()["error"]["code"] == mcp_main.UNAVAILABLE


async def test_a2_bad_bearer_while_hit_is_down_is_503_not_401(client, w, limits_hit_down):
    """A2. Wrong implementation: the failed-auth add path swallowing the
    error and answering 401."""
    r = await _post(client, _msg("ping"), token=None)
    assert r.status_code == 503 and r.json()["error"]["code"] == mcp_main.UNAVAILABLE
    r = await _post(client, _msg("ping"), token="not-a-token")
    assert r.status_code == 503


async def test_a2_limits_db_down_is_503_on_the_first_check(client, w, limits_db_down):
    for token in ("write", None):
        r = await _post(client, _msg("ping"), token=token)
        assert r.status_code == 503, token
        assert r.json()["error"]["code"] == mcp_main.UNAVAILABLE


async def test_gate_6_is_a_no_op_in_app(factory, w, _autouse_fake_redis):
    """GUARD: the in-app channel carries no token, so gate 6 never refuses it
    (its bounds are the turn meter and the chat limits)."""
    async with factory() as db:
        u = await db.get(User, w["user"])
        out = await registry.invoke(db, u, "accounts_list", {}, channel="in_app")
    assert "data" in out


# ── preview, confirm and auto over the wire ──────────────────────────────

async def test_preview_then_confirm_over_mcp(client, w, factory):
    r = await _call(client, "budgets_update_amount",
                    {"budget_id": w["budget"], "amount": "120.00"})
    staged = r.json()["result"]["structuredContent"]["data"]
    assert staged["requires_confirmation"] is True
    async with factory() as db:
        assert (await db.get(Budget, w["budget"])).amount == Decimal("100.00")
    r = await _call(client, "confirm_action", {"action_id": staged["action_id"]})
    res = r.json()["result"]
    assert res["isError"] is False, res
    async with factory() as db:
        assert (await db.get(Budget, w["budget"])).amount == Decimal("120.00")
    assert await _count(factory, w) == 2
    # The text block carries the same JSON for clients that ignore structuredContent.
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]


async def test_fa1_auto_token_over_mcp_never_executes_a_sensitive_tool(client, w, factory, scratch):
    """FENCE F-A1 (transport). Wrong implementation: the front door deciding
    auto from the token scope alone and executing whatever it names."""
    calls = scratch("sens_tool", risk="sensitive")
    r = await _call(client, "sens_tool", {"budget_id": w["budget"]}, token="auto")
    data = r.json()["result"]["structuredContent"]["data"]
    assert data["requires_confirmation"] is True and calls == []
    async with factory() as db:
        row = await db.get(AgentPendingAction, data["action_id"])
        assert (row.status.value, row.mode.value) == ("pending", "confirm")


async def test_auto_token_executes_a_write_in_the_same_call(client, w, factory):
    r = await _call(client, "budgets_update_amount",
                    {"budget_id": w["budget"], "amount": "130.00"}, token="auto")
    assert r.json()["result"]["isError"] is False, r.text
    async with factory() as db:
        assert (await db.get(Budget, w["budget"])).amount == Decimal("130.00")


async def test_tool_errors_are_results_not_protocol_errors(client, w):
    r = await _call(client, "accounts_list", {"bogus": 1})
    res = r.json()["result"]
    assert r.status_code == 200 and res["isError"] is True
    assert res["structuredContent"]["code"] == "invalid_arguments"
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
    r = await _call(client, "no_such_tool", {})
    assert r.json()["result"]["structuredContent"]["code"] == "unknown_tool"


# ── protocol guards ──────────────────────────────────────────────────────

async def test_initialize_negotiates_the_version(client, w):
    r = await _post(client, _msg("initialize"))
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"]
    r = await _post(client, _msg("initialize", {"protocolVersion": "1999-01-01",
                                                "capabilities": {}, "clientInfo": {}}))
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"  # our latest
    r = await _post(client, _msg("initialize", {"protocolVersion": "2025-11-25",
                                                "capabilities": {}, "clientInfo": {}}))
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


async def test_protocol_error_paths(client, w):
    r = await _post(client, None, raw="{not json")
    assert r.json()["error"]["code"] == -32700
    r = await _post(client, [_msg("ping")])
    assert r.json()["error"]["code"] == -32600
    r = await _post(client, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert r.json()["error"]["code"] == -32601
    r = await _post(client, _msg("notifications/initialized"))
    assert r.status_code == 202 and r.content == b""
    r = await _post(client, _msg("ping"))
    assert r.json() == {"jsonrpc": "2.0", "id": r.json()["id"], "result": {}}
    r = await _post(client, None, raw="x" * (65 * 1024))
    assert r.status_code == 413
    r = await _call(client, "accounts_list", "not-an-object")
    assert r.json()["error"]["code"] == -32602
    r = await _post(client, _msg("tools/call", {"name": ["accounts_list"], "arguments": {}}))
    assert r.json()["error"]["code"] == -32602
    r = await _post(client, {"jsonrpc": "2.0", "id": 9, "result": {}})  # a client's response
    assert r.status_code == 202 and r.content == b""


async def test_body_cap_counts_the_stream_not_content_length(client, w):
    async def chunks():
        for _ in range(70):
            yield b"x" * 1024

    headers = {"authorization": f"Bearer {_tok('write')}", "content-type": "application/json"}
    r = await client.post("/mcp", content=chunks(), headers=headers)
    assert r.status_code == 413


async def test_failed_auth_ceiling_is_per_ip_and_valid_tokens_do_not_count(client, w, frozen):
    """FENCE (ruling 5, folded). Wrong implementations: no ceiling at all, a
    ceiling that counts VALID requests (hosted clients sharing egress IPs
    would throttle each other), and one keyed globally instead of per IP."""
    ip = "198.51.100.9"
    for _ in range(299):
        assert (await _post(client, _msg("ping"), token=None, ip=ip)).status_code == 401
    for _ in range(30):  # valid traffic from the same IP is not counted
        assert (await _post(client, _msg("ping"), ip=ip)).status_code == 200
    assert (await _post(client, _msg("ping"), token=None, ip=ip)).status_code == 401  # 300th
    r = await _post(client, _msg("ping"), token=None, ip=ip)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    # A tripped IP refuses before auth, valid bearer or not (no DB lookups).
    assert (await _post(client, _msg("ping"), ip=ip)).status_code == 429
    assert (await _post(client, _msg("ping"), token=None, ip="198.51.100.10")).status_code == 401


async def test_every_request_draws_on_the_token_request_bucket(
    client, w, factory, frozen
):
    """FENCE (folded). Wrong implementation: only tools/call token-limited, so
    a token spread over many IPs runs unbounded auth + entitlement queries
    through initialize / ping / tools/list, notifications, bad bodies, or a
    non-entitled org."""
    rate_limit_db.hit(f"agent:tok:{w['tok']['write']}:req:min", 60, amount=300)
    for m in ["initialize", "ping", "tools/list", "notifications/initialized", "tools/call"]:
        r = await _post(client, _msg(m), token="write", ip=f"192.0.2.{len(m)}")
        assert r.status_code == 429, m
    assert (await _post(client, None, raw="{not json")).status_code == 429
    assert (await _post(client, _msg("ping"), token="write2")).status_code == 200
    assert await _count(factory, w) == 0


async def test_db_outage_on_auth_is_503_never_401(client, w, monkeypatch):
    """FENCE (folded). Wrong implementation: a catch-all mapping any auth
    failure to 401, so a DB blip tells every OAuth client its credential is
    dead (it then refreshes or discards it)."""
    from sqlalchemy.exc import OperationalError

    from app.agent import auth

    async def boom(*a, **k):
        raise OperationalError("select", {}, Exception("down"))

    monkeypatch.setattr(auth, "lookup_token", boom)
    r = await _post(client, _msg("ping"))
    assert r.status_code == 503 and "www-authenticate" not in r.headers


async def test_protocol_version_header(client, w):
    headers = {"authorization": f"Bearer {_tok('write')}", "content-type": "application/json"}
    for version, status in [("1999-01-01", 400), ("2025-06-18", 200), ("2025-11-25", 200)]:
        r = await client.post("/mcp", content=json.dumps(_msg("ping")),
                              headers={**headers, "mcp-protocol-version": version})
        assert r.status_code == status, version


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", f"Basic {_tok('write')}",
                                    _tok("write")])
async def test_only_a_bearer_scheme_authenticates(client, w, header):
    r = await client.post("/mcp", content=json.dumps(_msg("ping")),
                          headers={"authorization": header, "content-type": "application/json"})
    assert r.status_code == 401


async def test_get_mcp_is_405(client):
    assert (await client.get("/mcp")).status_code == 405


async def test_db_outage_at_the_door_is_503(client, w, monkeypatch):
    from sqlalchemy.exc import OperationalError

    async def boom(*a, **k):
        raise OperationalError("select", {}, Exception("down"))

    monkeypatch.setattr(mcp_main.feature_service, "get_entitlements", boom)
    r = await _post(client, _msg("ping"))
    assert r.status_code == 503 and "www-authenticate" not in r.headers


async def test_deeply_nested_body_is_a_parse_error_not_a_500(client, w):
    r = await _post(client, None, raw="[" * 60_000)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
