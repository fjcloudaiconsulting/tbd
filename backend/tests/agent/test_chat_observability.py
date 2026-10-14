"""TBD-617: what an assistant turn logs, and what it must never log.

Fixtures come from ``test_chat`` (same provider seam, real routing/ledger).
Capture is a handler on the ROOT stdlib logger: ``app.logging`` bridges every
structlog logger to stdlib, so one handler sees every module (``capture_logs``
is banned here, and binding a recorder to one module's logger could not prove
"logged nowhere").
"""
from __future__ import annotations

import dataclasses
import json
import httpx
import pytest
import structlog

from app.agent import registry
from app.config import settings as app_settings
from app.models.org_ai_credential import AiProvider
from app.services import agent_chat as chat
from test_chat import (  # noqa: F401  (fixtures)
    ASK, URL, _add, _as, _call, _env, _events, _ledger, _resp, client, engine, factory,
    provider, w,
)

SENT = "ZQ-SENTINEL-617"


async def _turn(client, **kw):
    return await client.post(URL, json=kw.pop("body", ASK), **kw)


async def test_round_events(factory, w, client, provider, logs):
    """FENCE R1. Wrong implementations: logging the model-supplied tool name;
    dropping the provider's tokens or the ledger id; one event per turn."""
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
    assert r1["stop"] is None
    assert (r2["prompt_tokens"], r2["ledger_id"], r2["tools"], r2["stop"]) == (222, ids[1], [], "ok")


async def test_stop_reasons_and_sse_code(factory, w, client, provider, logs, monkeypatch):
    """FENCE R2. ``stop`` names why the loop ended in that round; ``sse_code``
    is what the browser got. Wrong implementation: logging ``outcome`` before
    it is set, or an sse_code that is not the error event's code."""
    _as(factory, w["user"])
    # round limit: the 7th round never dispatches but is still reported
    provider(lambda n: _resp([_call("accounts_list", cid=f"c{n}")]))
    await _turn(client)
    rounds = logs.named("agent.turn.round")
    assert len(rounds) == 7 and rounds[-1]["stop"] == "round_limit"
    assert rounds[-1]["prompt_tokens"] is None and rounds[-1]["ledger_id"] is None
    assert [r["stop"] for r in rounds[:-1]] == [None] * 6
    assert logs.named("agent.turn.completed")[-1]["sse_code"] == "round_limit"

    # blank reply
    logs.events.clear()
    provider([_resp(content="  ")])
    await _turn(client)
    assert logs.named("agent.turn.round")[-1]["stop"] == "empty_reply"
    assert logs.named("agent.turn.completed")[-1]["sse_code"] == "empty_reply"

    # normal
    logs.events.clear()
    provider([_resp(content="hi")])
    await _turn(client)
    assert logs.named("agent.turn.completed")[-1]["sse_code"] == "ok"

    # preview
    logs.events.clear()
    provider([_resp([_call("budgets_update_amount",
                           {"budget_id": w["budget"], "amount": "120.00"})])])
    await _turn(client)
    assert logs.named("agent.turn.round")[-1]["stop"] == "preview"
    assert logs.named("agent.turn.completed")[-1]["sse_code"] == "preview"

    # a dispatch error: the code is the error event's code, tokens absent
    logs.events.clear()
    from app.services import ai_dispatch
    from app.services.ai_providers import AIProviderError

    async def boom(n):
        raise AIProviderError(code="provider_status_400", status_code=400)

    provider(boom)
    ev = _events((await _turn(client)).text)
    assert ("error", {"code": "provider_status_400"}) in ev
    last = logs.named("agent.turn.round")[-1]
    assert last["stop"] == "provider_status_400" and last["prompt_tokens"] is None
    assert last["ledger_id"] is None and last["est_tokens"] > 0
    assert logs.named("agent.turn.completed")[-1]["sse_code"] == "provider_status_400"


async def test_turn_events_carry_the_request_id(factory, w, client, provider, logs):
    """FENCE R3. The generator runs after the handler returned: the request's
    contextvars must still be bound. Wrong implementation: clearing/rebinding
    context in the stream, or logging from a detached task."""
    _as(factory, w["user"])
    provider([_resp([_call("accounts_list")]), _resp(content="hi")])
    await client.post(URL, json=ASK, headers={"X-Request-ID": "req-617-abc"})
    turn = [e for e in logs.events if str(e.get("event", "")).startswith("agent.turn.")
            or e.get("event") in ("ai.ledger.recorded", "ai.dispatch.success")]
    assert {e["event"] for e in turn} >= {
        "agent.turn.round", "agent.turn.completed", "ai.ledger.recorded"}
    assert [e["request_id"] for e in turn if "request_id" not in e or e["request_id"] != "req-617-abc"] == []


async def test_ledger_write_is_logged_and_linkable(factory, w, client, provider, logs):
    """FENCE R4. Wrong implementation: no event for the write, or one that
    omits the ledger id (the only join key to the turn's ``ledger_ids``)."""
    _as(factory, w["user"])
    provider([_resp([_call("accounts_list")], prompt_tokens=7), _resp(content="hi", prompt_tokens=9)])
    await _turn(client)
    rows = await _ledger(factory)
    rec = logs.named("ai.ledger.recorded")
    assert [(e["ledger_id"], e["feature_key"], e["success"], e["error_class"],
             e["prompt_tokens"], e["completion_tokens"]) for e in rec] == [
        (r.id, "chat", True, None, p, 5) for r, p in zip(rows, (7, 9))]


async def test_privacy_nothing_user_supplied_is_ever_logged(
    factory, w, client, logs, monkeypatch,
):
    """FENCE R5. One turn through the REAL OpenAI adapter where every content
    channel carries a sentinel (user message, history, tool arguments, tool
    result rows, assistant text, the provider's 400 message) plus an
    HTTPException string detail; EVERY event of EVERY module is searched.
    Wrong implementations: logging args/results/replies/messages, the
    provider's error message, or a string detail."""
    # the capture sees any module's structlog event (not vacuous)
    structlog.stdlib.get_logger("some.other.module").info("canary", v="CANARY-617")
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
