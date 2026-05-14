"""Pure-ASGI middleware that stamps baseline security headers onto every
HTTP response from the FastAPI backend.

**Why this middleware exists.** In the DO App Platform topology, the
ingress routes ``/api/*``, ``/health``, and ``/ready`` directly to the
backend component and routes everything else to the frontend (Next.js)
component. The Next.js config (``frontend/next.config.ts``) stamps HSTS,
CSP, X-Frame-Options, etc. on responses it owns — but those handlers
never see API traffic. Without this middleware, every ``/api/*`` response
returned at ``app.thebetterdecision.com`` left HSTS, X-Content-Type-
Options, X-Frame-Options, and Referrer-Policy unset, weakening the
trust profile of the entire app surface and breaking HSTS preload
eligibility (preload requires HSTS on every response from the host).

**Why pure ASGI, not BaseHTTPMiddleware.** Same rationale as
``RequestContextMiddleware`` in this package: ``BaseHTTPMiddleware``
wraps the downstream app in a ``StreamingResponse`` task, which both
breaks contextvar propagation and forces an extra response copy. Pure
ASGI lets us mutate the outbound ``http.response.start`` headers list
in-place with no extra overhead.

**Scope.** Only HTTP responses get headers stamped. Lifespan and
websocket scopes pass through untouched.

**HSTS value.** ``max-age=63072000; includeSubDomains; preload``
mirrors the frontend's Next.js config exactly — two years, all
subdomains, preload-eligible. The pair is intentional: a divergence
between the apex/app frontend and the API would defeat the preload
check.

**Conservative on the backend.** CSP is deliberately NOT stamped here.
JSON API responses don't render scripts/styles, so CSP on them is
moot, and an inconsistent CSP between API and HTML responses would
require coordinated maintenance for no defense gain. The frontend
owns CSP for the rendered surface; this middleware owns the headers
that apply universally to any HTTP response (HSTS, content-type
sniffing, framing, referrer policy).
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Baseline security headers stamped on every backend HTTP response.
# Names are lowercase bytes because ASGI conventionally lowercases
# header keys; values are bytes for the same reason.
#
# The HSTS value mirrors ``frontend/next.config.ts`` exactly. Drift
# between the two would break HSTS preload eligibility for the host.
_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"strict-transport-security",
        b"max-age=63072000; includeSubDomains; preload",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)

# Set of header names we own; lookup is O(1) for the "already present"
# check below. Built from the same tuple so they cannot drift.
_OWNED_HEADER_NAMES: frozenset[bytes] = frozenset(name for name, _ in _SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware. Append baseline security headers to every
    HTTP response that doesn't already carry them.

    A response that already sets one of these headers (e.g. a future
    handler that wants a tighter ``Referrer-Policy: no-referrer``)
    wins — we only fill gaps, we never override. That keeps the
    middleware safe to add as a blanket policy without breaking any
    handler that has a legitimate reason to deviate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                existing = {name for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
