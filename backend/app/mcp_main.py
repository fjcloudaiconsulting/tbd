"""The MCP server component (TBD-561): its own image, ``backend/Dockerfile.mcp``.

Exactly two routes: ``POST /mcp`` (MCP Streamable HTTP, stateless, JSON
responses only, no sessions, no server-initiated SSE) and ``GET /health``. It
does NOT import ``app.main``: no REST router, no scheduler, no migrations, no
LLM and no AI key. Tools run in-process through the shared registry, so every
gate, the preview-confirm engine and the ``mcp.calls`` meter are the same code
the in-app assistant uses.

Order on every ``POST /mcp``, whatever the JSON-RPC method (nothing is parsed
before the caller is known):

1. a per-IP ceiling on FAILED auth (300/min);
2. agent-token auth; every 401 carries ``resource_metadata`` (F-M4, F-O8);
   then every request draws on its token's request bucket;
3. the entitlement door: ``ai.agent`` on and an ``mcp.calls`` limit other than
   0, or 403 (F-E4). It admits nothing: only ``tools/call`` counts (F-Q5);
4. the body, capped, then JSON-RPC dispatch.

Hand-rolled rather than the ``mcp`` SDK (the SDK's 2.x needs a pydantic
upgrade; 1.x adds four packages and a sub-app that answers every method under
``/mcp`` and parses before our auth). Five methods, plain dicts.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app import redis_client
from app.agent import actions, registry
from app.agent.actions import window_key
from app.agent.auth import authenticate_agent_token, www_authenticate
from app.agent.registry import AGENT_FEATURE_KEY, ToolError
from app.database import async_session, engine
from app.logging import setup_logging
from app.rate_limit import get_client_ip
from app.services import feature_service

setup_logging()
logger = structlog.stdlib.get_logger(__name__)

# Tests point this at their own database.
session_factory = async_session

METER = "mcp.calls"
MAX_BODY = 64 * 1024
IP_AUTH_FAILURES_PER_MIN = 300
REQUESTS_PER_MIN = 300  # per token, every request (tools/call also draws gate 6)
SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")  # latest first
SERVER_INFO = {"name": "the-better-decision", "version": "1"}

# JSON-RPC codes. -32000..-32099 are server-defined.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = (
    -32700, -32600, -32601, -32602, -32603,
)
FORBIDDEN, RATE_LIMITED, UNAVAILABLE = -32003, -32029, -32050


def _rpc_error(
    status: int, code: int, message: str, *, id_: Any = None, data: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": err}, status, headers=headers)


def _unauthorized() -> JSONResponse:
    # The same body and header as every rejection in ``authenticate_agent_token``.
    return JSONResponse(
        {"detail": "Invalid or expired token"}, 401,
        headers={"WWW-Authenticate": www_authenticate()},
    )


def _bearer(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


async def _read_body(request: Request) -> bytes | None:
    """The body, or None past :data:`MAX_BODY` (never buffered whole)."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# ── tools/list ────────────────────────────────────────────────────────────

class _DecideArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str


_DECIDE = {
    "confirm_action": "Execute an action previously staged by a write tool, exactly as previewed.",
    "cancel_action": "Discard an action previously staged by a write tool.",
}


def _annotations(risk: str, scope: str) -> dict[str, bool]:
    """Hints only (A1.2); the server enforces, never the client."""
    if risk == "read":
        return {"readOnlyHint": True}
    if risk == "write":
        # A non-auto token only stages; an auto token executes.
        return {"readOnlyHint": False, "destructiveHint": scope == "agent:auto"}
    return {"readOnlyHint": False, "destructiveHint": True}


def _tool_list(scope: str) -> list[dict[str, Any]]:
    out = [
        {
            "name": spec.name, "description": spec.description,
            "inputSchema": spec.args.model_json_schema(),
            "annotations": _annotations(spec.risk, scope),
        }
        for spec in registry.all_tools()
        if scope != "agent:read" or spec.risk == "read"
    ]
    if scope != "agent:read":
        for name, description in _DECIDE.items():
            out.append({
                "name": name, "description": description,
                "inputSchema": _DecideArgs.model_json_schema(),
                "annotations": (
                    _annotations("sensitive", scope) if name == "confirm_action"
                    else {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
                ),
            })
    return out


# ── tools/call ────────────────────────────────────────────────────────────

def _content(payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, default=str)
    # structuredContent and the text block carry the same JSON.
    return {"content": [{"type": "text", "text": text}], "structuredContent": json.loads(text),
            "isError": is_error}


async def _tools_call(db, user, token, params: dict[str, Any]) -> dict[str, Any]:
    name, args = params["name"], params.get("arguments") or {}
    kw = {"channel": "mcp", "scope": token.scope, "api_token_id": token.id}
    if name in _DECIDE:
        if token.scope == "agent:read":
            # Refused here, before gate 6 and the meter: a read token never
            # decides an action (and is never shown these two tools).
            raise ToolError("scope_denied", "agent:read may call read tools only")
        try:
            action_id = _DecideArgs.model_validate(args).action_id
        except PydanticValidationError:
            raise ToolError("invalid_arguments", "action_id: a string is required") from None
        decide = registry.confirm_action if name == "confirm_action" else registry.cancel_action
        return await decide(db, user, action_id, **kw)
    return await registry.invoke(db, user, name, args, **kw)


# ── the endpoint ──────────────────────────────────────────────────────────

async def _ip_failures(ip: str, *, add: bool) -> int:
    """Failed-auth count for ``ip`` this minute (incremented when ``add``).

    Only FAILED auth is counted per IP: a valid token is limited per token,
    so valid traffic never fills it. ponytail: once tripped it refuses the
    whole IP before auth (no DB lookups), so one junk client behind a shared
    hosted egress IP can stall that IP's valid tokens for the minute; revisit
    with the OAuth ticket (authenticate first, refuse only failures). Async client, fails OPEN (a Redis outage must not lock out every
    harness, and the sync limiter would block the event loop per request)."""
    client = redis_client.get_client()
    if client is None:
        return 0
    key = window_key(f"mcp:authfail:ip:{ip}", 60)
    try:
        if not add:
            return int(await client.get(key) or 0)
        n = await client.incr(key)
        await client.expire(key, 60)
        return int(n)
    except RedisError:
        logger.warning("rate_limit.degraded", where="mcp.authfail")
        return 0


async def mcp_endpoint(request: Request) -> Response:
    ip = get_client_ip(request)
    if await _ip_failures(ip, add=False) >= IP_AUTH_FAILURES_PER_MIN:
        return _rpc_error(429, RATE_LIMITED, "too many failed attempts",
                          headers={"Retry-After": "60"})

    async def _refuse() -> JSONResponse:
        await _ip_failures(ip, add=True)
        return _unauthorized()

    raw = _bearer(request)
    if raw is None:
        logger.info("agent_token.auth_rejected", reason="no_bearer")
        return await _refuse()
    async with session_factory() as db:
        try:
            user, token = await authenticate_agent_token(request, raw, db, session_factory)
        except HTTPException:
            return await _refuse()
        except SQLAlchemyError:
            # Never a 401 for an outage: an OAuth client would discard a
            # good credential. No WWW-Authenticate.
            logger.exception("mcp.auth_unavailable")
            return _rpc_error(503, UNAVAILABLE, "temporarily unavailable")

        # Every authenticated request draws on the token's request bucket
        # (fails open), so a token spread over many IPs is bounded whatever it
        # sends. Separate from gate 6's call bucket, so tools/call is never
        # charged twice against one limit.
        try:
            await actions._hit(f"agent:tok:{token.id}:req:min", REQUESTS_PER_MIN, 60,
                               "token_rate_limited")
        except ToolError as exc:
            if exc.code != "limits_unavailable":
                return _rpc_error(429, RATE_LIMITED, "rate limited", headers={"Retry-After": "60"},
                                  data={"code": exc.code, "detail": exc.detail, "data": {}})
            logger.warning("rate_limit.degraded", where="mcp.requests", api_token_id=token.id)

        try:
            ent = await feature_service.get_entitlements(db, user.org_id)
        except SQLAlchemyError:
            logger.exception("mcp.entitlements_unavailable")
            return _rpc_error(503, UNAVAILABLE, "temporarily unavailable")
        if not ent.features.get(AGENT_FEATURE_KEY) or ent.limits[METER].limit == 0:
            # 403, not 401: the token is fine, so an OAuth client must not
            # re-authenticate in a loop. No WWW-Authenticate.
            return _rpc_error(403, FORBIDDEN, "feature not enabled", data={
                "code": "feature_not_enabled", "feature_key": AGENT_FEATURE_KEY, "meter": METER,
            })

        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in SUPPORTED_VERSIONS:
            return _rpc_error(400, INVALID_REQUEST, f"unsupported protocol version {version}")

        declared = request.headers.get("content-length", "")
        body = None if declared.isdigit() and int(declared) > MAX_BODY else await _read_body(request)
        if body is None:
            return _rpc_error(413, INVALID_REQUEST, "request body too large")
        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):  # RecursionError: deeply nested input
            return _rpc_error(400, PARSE_ERROR, "parse error")
        if isinstance(msg, list):
            return _rpc_error(400, INVALID_REQUEST, "batch requests are not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _rpc_error(400, INVALID_REQUEST, "invalid request")
        if "method" not in msg and ("result" in msg or "error" in msg):
            return Response(status_code=202)  # a client's response: nothing to say
        method = msg.get("method")
        if not isinstance(method, str):
            return _rpc_error(400, INVALID_REQUEST, "invalid request")
        if "id" not in msg:
            return Response(status_code=202)  # any notification
        id_ = msg["id"]
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return _rpc_error(200, INVALID_PARAMS, "params must be an object", id_=id_)
        params = params or {}

        if method == "initialize":
            asked = params.get("protocolVersion")
            result: dict[str, Any] = {
                # Unsupported: answer the latest we speak; the client decides.
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_list(token.scope)}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str) or not isinstance(
                params.get("arguments", {}), (dict, type(None))
            ):
                return _rpc_error(200, INVALID_PARAMS, "name and object arguments required", id_=id_)
            try:
                result = _content(await _tools_call(db, user, token, params), False)
            except ToolError as exc:
                data = {"code": exc.code, "detail": exc.detail, "data": exc.data}
                if exc.code == "token_rate_limited":
                    return _rpc_error(429, RATE_LIMITED, "rate limited", id_=id_, data=data,
                                      headers={"Retry-After": "60"})
                if exc.code == "limits_unavailable":
                    return _rpc_error(503, UNAVAILABLE, "temporarily unavailable", id_=id_,
                                      data=data)
                result = _content(data, True)
        else:
            return _rpc_error(200, METHOD_NOT_FOUND, f"method not found: {method}", id_=id_)
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


@asynccontextmanager
async def lifespan(app: Starlette):
    yield
    await redis_client.close_client()
    await engine.dispose()


app = Starlette(
    routes=[
        Route("/mcp", mcp_endpoint, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ],
    lifespan=lifespan,
)
