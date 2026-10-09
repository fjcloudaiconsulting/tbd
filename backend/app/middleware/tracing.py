"""The HTTP SERVER span (INFRA-105; Ziftbook's pattern, as pure ASGI).

Pure ASGI, not ``@app.middleware("http")``: BaseHTTPMiddleware runs the app in another task, so
contextvars bound in a handler never reach the outer scope (see request_context.py), and it returns
at the response headers, which would end the span before a streaming response finishes.

Attributes are an allowlist: method, route template, status and, on error, error.type. Never
``url.path`` or ``url.query``: they carry invite tokens and OAuth codes.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import tracing

METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


class TracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in tracing.HEALTH_PATHS:
            await self.app(scope, receive, send)
            return

        # traceparent only: tracestate and baggage are client-controlled text.
        parent = tracing.PROPAGATOR.extract(
            {"traceparent": Headers(scope=scope).get("traceparent", "")}
        )
        method = scope["method"] if scope["method"] in METHODS else "_OTHER"
        status = 500  # the app raised before starting a response
        raised = False

        async def send_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with tracing.span(
            method, SpanKind.SERVER, {"http.request.method": method}, context=parent
        ) as current:
            try:
                await self.app(scope, receive, send_status)
            except Exception:
                raised = True  # tracing.span sets the status and error.type from the class
                raise
            finally:
                # The router sets scope["route"] on this same dict; unset when nothing matched.
                route = getattr(scope.get("route"), "path", None) or "unmatched"
                current.update_name(f"{method} {route}")
                current.set_attribute("http.route", route)
                current.set_attribute("http.response.status_code", status)
                if status >= 500 and not raised:
                    current.set_status(trace.Status(trace.StatusCode.ERROR))
                    current.set_attribute("error.type", str(status))
