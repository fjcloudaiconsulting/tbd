"""Pure-ASGI middleware that binds a per-request correlation context.

**Why pure ASGI, not BaseHTTPMiddleware.** Starlette's
``BaseHTTPMiddleware`` runs the wrapped app on a separate task via an
internal ``StreamingResponse`` bridge. ``contextvars`` set inside the
handler (e.g. ``user_id`` / ``org_id`` / ``role`` bound by
``deps.get_current_user``) live in that downstream task's context and
do NOT propagate back into the middleware's outer scope. Anything the
outer scope emits afterward — most importantly uvicorn's access log
line, which fires from there — would silently miss the auth fields,
defeating the whole point of L4.9. Pure-ASGI middleware shares the
caller's task and contextvar pool, so a ``bind_contextvars`` inside
the handler stays visible to the outer scope, and the access log
inherits both ``request_id`` and the auth context for free.

Per HTTP request, this middleware:

- Clears structlog's contextvars (fresh per-request scope; never bleed
  state from a previous request that crashed mid-handler).
- Reads ``X-Request-Id`` from the inbound headers if present and it
  passes a strict character-set + length policy; otherwise generates
  a UUID4 hex. Either way the value is bound onto structlog
  contextvars under the ``request_id`` key.
- Stashes the same value on ``scope["state"]["request_id"]`` so
  ``request.state.request_id`` works in any FastAPI handler.
- Echoes the value back as ``X-Request-Id`` on the response so
  callers (frontend, ops) can correlate.

User / org / role context is bound LATER, inside
``deps.get_current_user``. With pure-ASGI semantics here, that
binding stays visible to the outer scope — including uvicorn.access.
"""
from __future__ import annotations

import re
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Bounded length on the inbound side. Past this we generate a fresh
# UUID instead of trusting the caller's value — keeps log size sane
# and shuts the door on absurd header values.
_MAX_INBOUND_LENGTH = 64

# Strict character set: ASCII word chars (letters, digits, underscore),
# dot, and hyphen. Mirrors the frontend proxy.ts policy exactly. The
# ``re.ASCII`` flag is mandatory: Python's ``\w`` is Unicode-aware by
# default and would preserve ``é``, ``漢字``, etc., while the frontend's
# JavaScript ``\w`` is ASCII-only. Production API requests bypass the
# frontend proxy and hit the backend directly, so this regex is the
# real trust boundary — character-set parity with the frontend has to
# be exact.
_SAFE_ID_RE = re.compile(r"^[\w.\-]+$", re.ASCII)


def _coerce_request_id(raw: str | None) -> str:
    """Return a safe-to-log request id. Reuses the inbound value when
    it passes both the length cap and the safe-character regex;
    otherwise generates a fresh UUID4 hex.
    """
    if raw and 0 < len(raw) <= _MAX_INBOUND_LENGTH and _SAFE_ID_RE.fullmatch(raw):
        return raw
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Pure-ASGI implementation. Do NOT subclass BaseHTTPMiddleware
    here — see the module docstring for the contextvar-propagation
    rationale.
    """

    def __init__(self, app: ASGIApp, header_name: str = "x-request-id") -> None:
        self.app = app
        self.header_name = header_name
        # Cached lowercase bytes form for the ASGI scope-headers
        # comparison; ASGI headers are bytes and conventionally
        # lowercased.
        self._header_bytes = header_name.encode("latin-1").lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan / websocket — pass through untouched. structlog
            # contextvars don't apply here.
            await self.app(scope, receive, send)
            return

        # Fresh per-request scope. A handler that crashed mid-flight
        # in the previous request must not leave residue here.
        structlog.contextvars.clear_contextvars()

        # Find the inbound X-Request-Id (case-insensitive on the
        # header name, per the HTTP spec).
        inbound: str | None = None
        for name, value in scope.get("headers", []):
            if name == self._header_bytes:
                try:
                    inbound = value.decode("latin-1")
                except (UnicodeDecodeError, AttributeError):
                    inbound = None
                break

        request_id = _coerce_request_id(inbound)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Make request.state.request_id available without re-deriving.
        # ``scope.setdefault`` keeps any state dict Starlette already
        # populated.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        # Wrap ``send`` so we can inject the X-Request-Id header on
        # the way out. ``http.response.start`` is the only message
        # type that carries headers; everything else passes through.
        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                headers.append(
                    (self._header_bytes, request_id.encode("latin-1"))
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_header)
