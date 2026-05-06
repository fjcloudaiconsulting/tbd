"""Tests for the L4.9 request-context middleware + auth-time binding.

Three concerns under test:

1. Every HTTP request gets a ``request_id`` bound onto structlog's
   contextvars and echoed back as the ``X-Request-Id`` response
   header. An inbound ``X-Request-Id`` is preserved if it passes
   the length + safe-character policy.
2. Once auth resolves through ``deps.get_current_user``, the same
   contextvar pool also carries ``user_id`` / ``org_id`` / ``role``.
3. **Critical**: the auth context bound INSIDE a handler stays
   visible to outer scopes that emit logs after the handler returns
   (uvicorn.access in particular). This is the regression that
   ``BaseHTTPMiddleware`` introduces and that pure-ASGI middleware
   avoids.
"""
from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.request_context import RequestContextMiddleware


def _build_app(captured: list[dict]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/echo")
    async def echo(request: Request):
        captured.append(dict(structlog.contextvars.get_contextvars()))
        return {"request_id_state": getattr(request.state, "request_id", None)}

    return app


def test_middleware_generates_request_id_when_missing():
    captured: list[dict] = []
    with TestClient(_build_app(captured)) as client:
        res = client.get("/echo")
    assert res.status_code == 200
    rid = res.headers.get("x-request-id")
    assert rid and len(rid) == 32
    assert captured[-1].get("request_id") == rid
    assert res.json()["request_id_state"] == rid


def test_middleware_preserves_inbound_request_id():
    captured: list[dict] = []
    with TestClient(_build_app(captured)) as client:
        res = client.get("/echo", headers={"X-Request-Id": "trace-abc-123"})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "trace-abc-123"
    assert captured[-1].get("request_id") == "trace-abc-123"


def test_middleware_rejects_overlong_inbound_request_id():
    captured: list[dict] = []
    huge = "x" * 200
    with TestClient(_build_app(captured)) as client:
        res = client.get("/echo", headers={"X-Request-Id": huge})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") != huge
    assert len(res.headers["x-request-id"]) == 32


def test_middleware_rejects_inbound_id_with_spaces():
    """Spaces are not in the safe character set [\\w.\\-]+. A header
    value the platform accepts but our policy doesn't must be replaced
    with a fresh id — keeps grepability and avoids contradicting the
    'same regex as the frontend' contract.
    """
    captured: list[dict] = []
    with TestClient(_build_app(captured)) as client:
        res = client.get("/echo", headers={"X-Request-Id": "abc inject"})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") != "abc inject"
    assert " " not in res.headers["x-request-id"]


def test_middleware_rejects_inbound_id_with_other_unsafe_chars():
    """Quotes, slashes, percents, semicolons, braces — anything outside
    word/dot/hyphen — are replaced. Mirrors the frontend regex.
    """
    captured: list[dict] = []
    bad_values = ['abc"def', "abc/def", "abc%20def", "abc;rm", "abc{def}"]
    with TestClient(_build_app(captured)) as client:
        for bad in bad_values:
            captured.clear()
            res = client.get("/echo", headers={"X-Request-Id": bad})
            assert res.status_code == 200, bad
            assert res.headers.get("x-request-id") != bad, bad
            assert len(res.headers["x-request-id"]) == 32, bad


def test_coerce_rejects_non_ascii_word_chars():
    """Python's ``\\w`` is Unicode-aware by default; the frontend's
    JavaScript ``\\w`` is ASCII-only. The backend regex carries the
    ``re.ASCII`` flag so both edges reject the same shapes — letters
    with diacritics, CJK, etc. all get replaced with a fresh UUID.

    Tested at the coercion-function level rather than via TestClient
    because httpx blocks non-ASCII header strings at the client side
    (UnicodeEncodeError before transit). The middleware itself decodes
    raw latin-1 bytes from ``scope["headers"]``, so a less strict
    client could still deliver these byte sequences in production —
    this test exercises the regex on the values the middleware would
    actually see.
    """
    from app.middleware.request_context import _coerce_request_id

    for bad in ("é", "trace-é", "漢字", "café", "naïve"):
        coerced = _coerce_request_id(bad)
        assert coerced != bad, bad
        assert len(coerced) == 32, bad


def test_middleware_clears_contextvars_between_requests():
    """A second request starts with a fresh scope — no stale state."""
    captured: list[dict] = []
    with TestClient(_build_app(captured)) as client:
        client.get("/echo", headers={"X-Request-Id": "first.req"})
        client.get("/echo", headers={"X-Request-Id": "second.req"})
    assert captured[0].get("request_id") == "first.req"
    assert captured[1].get("request_id") == "second.req"
    assert "first.req" not in captured[1].values()


# ── Critical regression: handler-bound contextvars survive ────────────────


def test_handler_bound_contextvars_survive_to_outer_scope():
    """The whole point of the pure-ASGI rewrite: a contextvar bound
    INSIDE a handler must remain visible to the OUTER scope after the
    handler returns. ``uvicorn.access`` runs from the outer scope, so
    without this property auth fields silently fail to land on
    access-log lines.

    Probe pattern: a small inner middleware captures contextvars
    AFTER the inner app finishes. With pure-ASGI middleware the probe
    sees handler-bound contextvars; with ``BaseHTTPMiddleware`` it
    would not.

    To insulate the test from any quirks of FastAPI's internal
    middleware stack (exception middleware, dependency machinery), the
    bottom of the stack is a raw ASGI app — same surface uvicorn calls.
    The handler binds the same contextvars ``deps.get_current_user``
    binds in the real app, then completes a normal HTTP response.
    """
    post_handler_snapshots: list[dict] = []

    class _ProbeMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)
            # Captured AFTER the inner app finishes. With pure-ASGI
            # middleware this still sees handler-bound contextvars;
            # with BaseHTTPMiddleware it would not.
            post_handler_snapshots.append(
                dict(structlog.contextvars.get_contextvars())
            )

    async def asgi_handler(scope, receive, send):
        # Simulate ``deps.get_current_user`` binding auth context.
        structlog.contextvars.bind_contextvars(
            user_id=42, org_id=7, role="owner"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    # Compose the stack manually: RequestContextMiddleware →
    # _ProbeMiddleware → raw asgi_handler. No FastAPI involved, so we
    # measure only the property the production middleware needs to
    # provide. Skip the ``with`` context manager so TestClient doesn't
    # drive the lifespan protocol — the raw asgi_handler only handles
    # the http scope (uvicorn handles lifespan separately in prod).
    app = RequestContextMiddleware(_ProbeMiddleware(asgi_handler))
    client = TestClient(app)
    res = client.get("/echo", headers={"X-Request-Id": "trace.test.l49"})
    assert res.status_code == 200
    assert res.headers.get("x-request-id") == "trace.test.l49"

    snapshot = post_handler_snapshots[-1]
    # request_id was bound by the outer RequestContextMiddleware before
    # the handler ran — visible in the post-handler scope.
    assert snapshot.get("request_id") == "trace.test.l49"
    # user_id / org_id / role were bound by the handler. With pure-ASGI
    # middleware they survive to the outer scope. This is the
    # regression assertion.
    assert snapshot.get("user_id") == 42, (
        "handler-bound user_id did not propagate to outer scope — "
        "uvicorn.access would miss it. Pure-ASGI middleware required."
    )
    assert snapshot.get("org_id") == 7
    assert snapshot.get("role") == "owner"


# ── Auth-time context binding inside the real dep ─────────────────────────


import pytest

import app.deps as deps_module
from app.deps import get_current_user
from app.models.user import Role, User
from fastapi.security import HTTPAuthorizationCredentials


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeAsyncSession:
    def __init__(self, value):
        self._value = value

    async def execute(self, _stmt):
        return _FakeResult(self._value)


def _make_user(**overrides) -> User:
    base = {
        "id": 7,
        "org_id": 42,
        "username": "alice",
        "email": "alice@example.com",
        "password_hash": "x",
        "role": Role.OWNER,
        "is_superadmin": False,
        "is_active": True,
    }
    base.update(overrides)
    return User(**base)


@pytest.mark.asyncio
async def test_get_current_user_binds_authenticated_context(monkeypatch) -> None:
    structlog.contextvars.clear_contextvars()
    user = _make_user(id=7, org_id=42, role=Role.OWNER)
    monkeypatch.setattr(
        deps_module, "decode_token",
        lambda _t: {"sub": "7", "type": "access"},
    )

    resolved = await get_current_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="signed-token"),
        _FakeAsyncSession(user),
    )
    assert resolved is user
    bound = structlog.contextvars.get_contextvars()
    assert bound.get("user_id") == 7
    assert bound.get("org_id") == 42
    assert bound.get("role") == "owner"
    structlog.contextvars.clear_contextvars()
