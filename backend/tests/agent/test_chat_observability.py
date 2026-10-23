"""TBD-617: what an assistant turn logs, and what it must never log.

Fixtures come from ``test_chat`` (same provider seam, real routing/ledger).
Capture is a handler on the ROOT stdlib logger: ``app.logging`` bridges every
stdlib logger, so one handler sees every module (``log_lines`` captures the
root handler, so "logged nowhere" can be asserted).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import httpx
import pytest
import logging

from app.agent import registry
from app.config import settings as app_settings
from app.models.org_ai_credential import AiProvider
from app.models.user import User
from app.services import agent_chat as chat
from tests.conftest import LogView
from test_chat import (  # noqa: F401  (fixtures)
    ASK, URL, _add, _as, _budget, _call, _env, _events, _ledger, _resp, client, engine, factory,
    provider, w, limiter,
)

SENT = "ZQ-SENTINEL-617"


async def _turn(client, **kw):
    return await client.post(URL, json=kw.pop("body", ASK), **kw)


async def test_round_events(factory, w, client, provider, log_lines):
    """FENCE R1. Wrong implementations: logging the model-supplied tool name;
    dropping the provider's tokens or the ledger id; one event per turn."""
    logs = LogView(log_lines)
    _as(factory, w["user"])
    adapter = provider([
        _resp([_call("accounts_list", cid="a"), _call("evil_tool_" + SENT, cid="b"),
               _call("budgets_update_amount", {"budget_id": w["budget"], "amount": "-1"}, "c")],
              prompt_tokens=111),
        _resp(content="done", prompt_tokens=222),
    ])
    await _turn(client)
    rounds = logs.named("agent.turn.round")
    ids = [r.id for r in await _ledger(factory)]
    assert [r["round"] for r in rounds] == [1, 2]
    r1, r2 = rounds
    assert r1["est_tokens"] == chat._estimate(adapter.seen[0], adapter.tools[0])
    assert (r1["prompt_tokens"], r1["completion_tokens"], r1["ledger_id"]) == (111, 5, ids[0])
    assert r1["tools"] == [
        {"name": "accounts_list", "ok": True, "code": None, "rows": 1},
        {"name": "unknown_tool", "ok": False, "code": "unknown_tool", "rows": None},
        {"name": "budgets_update_amount", "ok": False, "code": "invalid_arguments", "rows": None},
    ]
    assert r1.get("stop") is None
    assert (r2["prompt_tokens"], r2["ledger_id"], r2["tools"], r2.get("stop")) == (222, ids[1], [], "ok")


def _seq(logs):
    """(round, stop) of every round event, plus the turn's completed record; and
    the invariant that one event is logged per dispatched round."""
    rounds = logs.named("agent.turn.round")
    (done,) = logs.named("agent.turn.completed")
    assert len(rounds) == done["rounds"], "round events != dispatched rounds"
    return [(r["round"], r.get("stop")) for r in rounds], done


async def _run(client, logs):
    logs.clear()
    await _turn(client)
    return _seq(logs)


async def test_round_events_per_dispatched_round_only(
    factory, w, client, provider, log_lines, monkeypatch,
):
    """FENCE R2. Exactly one event per DISPATCHED round, whatever ended the turn;
    the stop of a turn that ends before a dispatch lives on ``completed`` only.
    Wrong implementations: a phantom event for round_limit/turn_timeout/
    input_budget, a missing event on one exit path, a duplicate."""
    logs = LogView(log_lines)
    _as(factory, w["user"])
    spin = lambda n: _resp([_call("accounts_list", cid=f"c{n}")])  # noqa: E731

    provider(spin)
    seq, done = await _run(client, logs)
    assert seq == [(i, None) for i in range(1, 7)]
    assert (done["outcome"], done.get("sse_code")) == ("round_limit", "round_limit")

    limiter.reset()
    provider([_resp(content="  ")])
    seq, done = await _run(client, logs)
    assert seq == [(1, "empty_reply")] and done["sse_code"] == "empty_reply"

    limiter.reset()
    provider([_resp(content="hi")])
    seq, done = await _run(client, logs)
    assert seq == [(1, "ok")] and done.get("sse_code") is None  # no error event was sent

    limiter.reset()
    provider([_resp([_call("budgets_update_amount", {"budget_id": w["budget"], "amount": "120.00"})])])
    seq, done = await _run(client, logs)
    assert seq == [(1, "preview")] and done.get("sse_code") is None

    # a failed dispatch: code is the error event's, tokens absent, the failed ledger row linked
    limiter.reset()
    from app.services.ai_providers import AIProviderError

    async def boom(n):
        raise AIProviderError(code="provider_status_400", status_code=400)

    provider(boom)
    seq, done = await _run(client, logs)
    assert seq == [(1, "provider_status_400")] and done["sse_code"] == "provider_status_400"
    (r,) = logs.named("agent.turn.round")
    assert r.get("prompt_tokens") is None and r["est_tokens"] > 0
    assert r["ledger_id"] == (await _ledger(factory))[-1].id

    # an internal error: the in-flight round is still reported
    limiter.reset()
    provider([_resp([_call("accounts_list")])])
    real = chat._rows
    monkeypatch.setattr(chat, "_rows", lambda d: 1 / 0)
    seq, done = await _run(client, logs)
    monkeypatch.setattr(chat, "_rows", real)
    assert seq == [(1, "internal_error")] and done["sse_code"] == "internal_error"

    # user_inactive
    limiter.reset()
    async def deactivate_then_call(n):
        async with factory._f() as db:
            (await db.get(User, w["user"])).is_active = False
            await db.commit()
        return _resp([_call("accounts_list")])

    provider(deactivate_then_call)
    seq, done = await _run(client, logs)
    assert seq == [(1, "user_inactive")] and done["sse_code"] == "user_inactive"


async def test_round_events_when_the_turn_ends_before_a_dispatch(
    factory, w, client, provider, log_lines, monkeypatch,
):
    """FENCE R2b. turn_timeout and input_budget: the dispatched rounds, no phantom."""
    logs = LogView(log_lines)
    _as(factory, w["user"])
    adapter = provider(lambda n: _resp([_call("accounts_list", cid=f"c{n}")]))
    await _turn(client)
    e = [chat._estimate(adapter.seen[i], adapter.tools[i]) for i in range(2)]
    limiter.reset()
    provider(lambda n: _resp([_call("accounts_list", cid=f"c{n}")]))
    _budget(monkeypatch, e[0] + e[1] - 1)
    seq, done = await _run(client, logs)
    assert seq == [(1, None)] and (done["outcome"], done["sse_code"]) == ("input_budget", "input_budget")

    limiter.reset()
    _budget(monkeypatch, 40_000)
    monkeypatch.setattr(app_settings, "ai_dispatch_timeout_s", 0.2)
    monkeypatch.setattr(chat, "TURN_SECONDS", 0.3)

    async def slow(n):
        await asyncio.sleep(0.15)
        return _resp([_call("accounts_list", cid=f"c{n}")])

    provider(slow)
    seq, done = await _run(client, logs)
    assert seq == [(1, None)] and (done["outcome"], done["sse_code"]) == ("turn_timeout", "turn_timeout")


async def _disconnect_at(marker: bytes, factory, w, provider, monkeypatch, logs, round2_late=False):
    """Drive the real ASGI app and park the stream when ``marker`` is sent, then
    report a client disconnect."""
    monkeypatch.setattr(chat, "KEEPALIVE_SECONDS", 0.05)
    _as(factory, w["user"])
    armed = asyncio.Event()

    async def late():
        armed.set()
        await asyncio.sleep(0.3)
        return _resp(content="late", prompt_tokens=222)

    if round2_late:
        provider(lambda n: _resp([_call("accounts_list")], prompt_tokens=111) if n == 1 else late())
    else:
        armed.set()
        provider([_resp([_call("accounts_list")], content="said", prompt_tokens=111)])
    body = json.dumps(ASK).encode()
    parked = asyncio.Event()
    msgs = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if msgs:
            return msgs.pop(0)
        await parked.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if marker in message.get("body", b"") and armed.is_set():
            parked.set()
            await asyncio.sleep(10)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": URL, "raw_path": URL.encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1), "server": ("t", 80),
        "headers": [(b"host", b"t"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    }
    from app.main import app
    await app(scope, receive, send)


async def test_round_events_on_disconnect(factory, w, provider, log_lines, monkeypatch):
    """FENCE R2c. A disconnect sends no error event (sse_code None), yet the
    in-flight round is reported once: with its tokens when the dispatch had
    returned, and a reaped dispatch with its ledger id."""
    logs = LogView(log_lines)
    await _disconnect_at(b"event: message", factory, w, provider, monkeypatch, logs)
    seq, done = _seq(logs)
    assert seq == [(1, "client_disconnected")]
    assert done["outcome"] == "client_disconnected" and done.get("sse_code") is None
    assert logs.named("agent.turn.round")[0]["prompt_tokens"] == 111

    logs.clear()
    limiter.reset()
    await _disconnect_at(b"keepalive", factory, w, provider, monkeypatch, logs, round2_late=True)
    seq, done = _seq(logs)
    assert seq == [(1, None), (2, "client_disconnected")] and done.get("sse_code") is None
    r2 = logs.named("agent.turn.round")[1]
    ids = [r.id for r in await _ledger(factory)]
    assert (r2["prompt_tokens"], r2["ledger_id"]) == (222, ids[-1])


async def test_turn_events_carry_the_request_id(factory, w, client, provider, log_lines):
    """FENCE R3. The generator runs after the handler returned: the request's
    contextvars must still be bound. Wrong implementation: clearing/rebinding
    context in the stream, or logging from a detached task."""
    logs = LogView(log_lines)
    _as(factory, w["user"])
    provider([_resp([_call("accounts_list")]), _resp(content="hi")])
    await client.post(URL, json=ASK, headers={"X-Request-ID": "req-617-abc"})
    turn = [e for e in logs.events if str(e.get("msg", "")).startswith("agent.turn.")
            or e.get("msg") in ("ai.ledger.recorded", "ai.dispatch.success")]
    assert {e["msg"] for e in turn} >= {
        "agent.turn.round", "agent.turn.completed", "ai.ledger.recorded"}
    assert [e["request_id"] for e in turn if "request_id" not in e or e["request_id"] != "req-617-abc"] == []


async def test_ledger_write_is_logged_and_linkable(factory, w, client, provider, log_lines):
    """FENCE R4. Wrong implementation: no event for the write, or one that
    omits the ledger id (the only join key to the turn's ``ledger_ids``)."""
    logs = LogView(log_lines)
    _as(factory, w["user"])
    provider([_resp([_call("accounts_list")], prompt_tokens=7), _resp(content="hi", prompt_tokens=9)])
    await _turn(client)
    rows = await _ledger(factory)
    rec = logs.named("ai.ledger.recorded")
    assert [(e["ledger_id"], e["feature_key"], e["success"], e.get("error_class"),
             e["prompt_tokens"], e["completion_tokens"]) for e in rec] == [
        (r.id, "chat", True, None, p, 5) for r, p in zip(rows, (7, 9))]


async def test_privacy_nothing_user_supplied_is_ever_logged(
    factory, w, client, log_lines, monkeypatch,
):
    """FENCE R5. One turn through the REAL OpenAI adapter where every content
    channel carries a sentinel (user message, history, tool arguments, tool
    result rows, assistant text, the provider's 400 message) plus an
    HTTPException string detail; EVERY event of EVERY module is searched.
    Wrong implementations: logging args/results/replies/messages, the
    provider's error message, or a string detail."""
    logs = LogView(log_lines)
    # the capture sees any module's event (not vacuous)
    logging.getLogger("app.some.other.module").info("canary", extra={"v": "CANARY-617"})
    assert "CANARY-617" in logs.text()

    spec = registry.get_tool("accounts_list")

    async def run(ctx, args):
        return {"items": [{"name": SENT, "amount": "123456.78"}], "note": SENT}

    monkeypatch.setitem(registry._TOOLS, "accounts_list", dataclasses.replace(spec, run=run))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={
                "model": "gpt-4o-mini", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [{"message": {"role": "assistant", "content": "said " + SENT,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {
                        "name": "accounts_list", "arguments": json.dumps({"note": SENT})}}]}}],
            })
        return httpx.Response(400, json={"error": {
            "message": SENT, "type": "invalid_request_error", "code": "context_length_exceeded"}})

    original = httpx.AsyncClient.__init__

    def patched(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        original(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    _as(factory, w["user"])
    body = {"messages": [{"role": "user", "content": "old " + SENT},
                         {"role": "assistant", "content": "older " + SENT},
                         {"role": "user", "content": "now " + SENT}]}
    ev = _events((await _turn(client, body=body)).text)
    assert ("error", {"code": "provider_status_400"}) in ev and len(calls) == 2
    # the provider's type/code (not its message) is on the failure event
    assert any(e.get("provider_reason") == "invalid_request_error" for e in logs.events)

    # an HTTPException string detail on an app route, with SENT in path and query
    from fastapi import HTTPException
    from app.main import app

    async def boom(item: str):
        raise HTTPException(400, detail="bad " + SENT)

    app.add_api_route("/__t617_priv/{item}", boom)
    try:
        r = await client.get(f"/__t617_priv/{SENT}?q={SENT}")
    finally:
        app.router.routes.pop()
    assert r.status_code == 400
    assert logs.named("http.client_error")
    assert SENT not in logs.text(), "a log line carried user-supplied content"


async def test_platform_provider_failure_is_not_the_users_to_fix(
    factory, w, client, provider, monkeypatch, log_lines,
):
    """FENCE TBD-662. A house-key (platform) provider_status_* must reach the user as
    ``platform_ai_unavailable`` (the key is not theirs), while the operator log keeps the
    original code. A BYOK turn keeps its provider code. Wrong implementation: mapping every
    provider_status_* regardless of who paid, or rewriting the logged outcome."""
    from app.services import ai_dispatch
    from app.services.ai_providers import AIProviderError

    logs = LogView(log_lines)
    _as(factory, w["user"])

    async def platform_429(*a, **k):
        exc = ai_dispatch.AIDispatchFailed("provider_status_429")
        exc.billing_source = "platform"
        raise exc

    real = ai_dispatch.call_llm_function
    monkeypatch.setattr("app.services.ai_dispatch.call_llm_function", platform_429)
    seq, done = await _run(client, logs)
    assert done["sse_code"] == "platform_ai_unavailable"
    assert logs.named("agent.turn.completed")[-1]["outcome"] == "provider_status_429"

    monkeypatch.setattr("app.services.ai_dispatch.call_llm_function", real)
    limiter.reset()

    async def boom(n):
        raise AIProviderError(code="provider_status_429", status_code=429)

    provider(boom)
    seq, done = await _run(client, logs)
    assert done["sse_code"] == "provider_status_429"
