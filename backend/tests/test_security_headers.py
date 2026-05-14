"""Tests for the L1.5 backend security-headers middleware.

The frontend's Next.js config stamps HSTS / X-Frame / CSP / etc. on
responses it owns, but the DO App Platform ingress routes ``/api/*``,
``/health``, and ``/ready`` directly at the backend component. Without
this middleware the backend's responses leave the host's HSTS preload
posture undefined.

Under test:

1. Every HTTP response carries HSTS, X-Content-Type-Options,
   X-Frame-Options, and Referrer-Policy.
2. The HSTS value mirrors the frontend's Next.js config exactly
   (drift between the two breaks preload eligibility).
3. A handler that already sets one of these headers wins — the
   middleware only fills gaps, never overrides.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from app.middleware.security_headers import SecurityHeadersMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/json")
    async def json_route():
        return {"hello": "world"}

    @app.get("/plain")
    async def plain():
        return PlainTextResponse("ok")

    @app.get("/handler-sets-referrer")
    async def handler_sets_referrer():
        # Handler chooses a tighter Referrer-Policy. Middleware must
        # not overwrite that choice.
        return JSONResponse(
            {"ok": True},
            headers={"Referrer-Policy": "no-referrer"},
        )

    return app


def test_health_response_carries_hsts():
    with TestClient(_build_app()) as client:
        res = client.get("/health")
    assert res.status_code == 200
    assert (
        res.headers.get("strict-transport-security")
        == "max-age=63072000; includeSubDomains; preload"
    )


def test_hsts_matches_frontend_value_exactly():
    """Drift between the frontend's Next.js HSTS value and the backend's
    HSTS value breaks HSTS preload eligibility (the preload check
    requires the SAME max-age and the SAME includeSubDomains setting on
    every response from the host). This test pins the value so anyone
    editing one side has to edit the other.
    """
    with TestClient(_build_app()) as client:
        res = client.get("/json")
    # Mirror of frontend/next.config.ts line 49-51 exactly.
    assert (
        res.headers.get("strict-transport-security")
        == "max-age=63072000; includeSubDomains; preload"
    )


def test_baseline_security_headers_present_on_every_response():
    with TestClient(_build_app()) as client:
        for path in ("/health", "/json", "/plain"):
            res = client.get(path)
            assert res.status_code == 200, path
            assert res.headers.get("strict-transport-security"), path
            assert res.headers.get("x-content-type-options") == "nosniff", path
            assert res.headers.get("x-frame-options") == "DENY", path
            assert (
                res.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
            ), path


def test_middleware_does_not_override_handler_set_header():
    """If a handler intentionally sets a tighter security header,
    the middleware must not overwrite it. Only gaps get filled.
    """
    with TestClient(_build_app()) as client:
        res = client.get("/handler-sets-referrer")
    assert res.status_code == 200
    # Handler's tighter Referrer-Policy wins.
    assert res.headers.get("referrer-policy") == "no-referrer"
    # Other headers still get stamped.
    assert res.headers.get("strict-transport-security")
    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.headers.get("x-frame-options") == "DENY"
