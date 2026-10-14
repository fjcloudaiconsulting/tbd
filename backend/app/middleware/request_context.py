"""Pure-ASGI middleware that opens the per-request log context and writes the access line.

**Why pure ASGI, not BaseHTTPMiddleware.** Starlette's ``BaseHTTPMiddleware`` runs the wrapped app
on a separate task, so a context bound inside the handler (``user_id`` / ``org_id`` / ``role`` bound
by ``deps.get_current_user``) never reaches this scope, and the access line written here after the
app returns would miss the auth fields. A pure-ASGI middleware shares the caller's task and
contextvar pool.

Per HTTP request, this middleware:

- Starts a fresh context holding only ``request_id``. ``CONTEXT.set``, never ``bind`` (which would
  merge into the previous request's context when two requests run in one task) and never reset
  (the line written below and the error handlers after it still need the context).
- Reads ``X-Request-Id`` from the inbound headers if present and it passes a strict character-set
  + length policy; otherwise generates a UUID4 hex. The same value is stashed on
  ``scope["state"]["request_id"]`` and echoed back as ``X-Request-Id``.
- Writes exactly one access line, in a ``finally`` (``status`` defaults to 500: Starlette's
  ServerErrorMiddleware is outside this one, so an unhandled error's response never passes our
  ``send``): method, route template, status and duration. Never the raw path, the query string or
  the client IP: they carry invite tokens, OAuth codes and addresses. Health probes log at DEBUG.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import logs, tracing
from app.middleware.tracing import METHODS


access_logger = logging.getLogger("app.access")

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
            # Lifespan / websocket: no request context applies.
            await self.app(scope, receive, send)
            return

        # Find the inbound X-Request-Id (case-insensitive on the header name, per the HTTP spec).
        inbound: str | None = None
        for name, value in scope.get("headers", []):
            if name == self._header_bytes:
                try:
                    inbound = value.decode("latin-1")
                except (UnicodeDecodeError, AttributeError):
                    inbound = None
                break

        request_id = _coerce_request_id(inbound)
        logs.CONTEXT.set({"request_id": request_id})

        # Make request.state.request_id available without re-deriving.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status = 500  # the app raised before starting a response

        # Wrap ``send`` to inject the X-Request-Id header and to see the status.
        async def send_with_header(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", ()))
                headers.append((self._header_bytes, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            fields: dict[str, object] = {
                "method": scope["method"] if scope["method"] in METHODS else "_OTHER",
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            # The router sets scope["route"] on this same dict; absent when nothing matched.
            if route := getattr(scope.get("route"), "path", None):
                fields["route"] = route
            level = logging.DEBUG if scope["path"] in tracing.HEALTH_PATHS else logging.INFO
            access_logger.log(level, "request", extra=fields)
