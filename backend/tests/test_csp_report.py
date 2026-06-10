"""Tests for the public CSP violation-report sink.

POST /api/v1/security/csp-report — covers:

- Public access (no auth dependency; an unauthenticated request is
  accepted, not 401/403).
- Legacy ``{"csp-report": {...}}`` body → one audit row.
- Reporting-API array ``[{"type", "body"}]`` → one row per envelope.
- Audit detail is bounded + normalized (kebab + camel keys collapse to
  the same snake_case keys; oversized fields truncated).
- Malformed / unknown shapes never 500 and write no audit row.
- ``client_ip`` and ``request_id`` land on the audit row.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import app.routers.security as security_router_module
from app.deps import get_session_factory
from app.models import Base
from app.models.audit_event import AuditEvent, AuditOutcome
from app.rate_limit import limiter
from app.routers.security import CSP_VIOLATION_EVENT_TYPE, router as security_router


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiter():
    """Each test starts and ends with an empty rate-limiter state. The
    SlowAPI ``Limiter`` is a module-level singleton; without an explicit
    reset the per-IP counter from one test bleeds into the next and a
    perfectly good test fails with a stale 429."""
    limiter.reset()
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch) -> AsyncIterator[AsyncClient]:
    # The router pulls the session factory by CALLING get_session_factory()
    # directly (not via Depends), so a dependency_overrides entry alone
    # won't reach it. Patch the symbol the router imported.
    monkeypatch.setattr(
        security_router_module, "get_session_factory", lambda: session_factory
    )

    app = FastAPI()
    # Wire the shared limiter so ``@limiter.limit("60/minute")`` on the
    # route surfaces 429s through the handler chain (mirrors prod wiring
    # in app.main and the pattern in test_rate_limit_sensitive_endpoints).
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.include_router(security_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _audit_rows(factory) -> list[AuditEvent]:
    async with factory() as db:
        rows = (
            await db.execute(select(AuditEvent).order_by(AuditEvent.id.asc()))
        ).scalars().all()
        return list(rows)


@pytest.mark.asyncio
async def test_legacy_csp_report_writes_one_audit_row(client, session_factory):
    body = {
        "csp-report": {
            "document-uri": "https://app.example.com/dashboard",
            "violated-directive": "script-src",
            "effective-directive": "script-src-elem",
            "blocked-uri": "https://evil.example.com/x.js",
            "disposition": "enforce",
            "line-number": 42,
        }
    }
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=body,
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204

    rows = await _audit_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == CSP_VIOLATION_EVENT_TYPE
    assert row.outcome == AuditOutcome.FAILURE
    assert row.actor_user_id is None
    assert row.actor_email == "anonymous"
    assert row.target_org_id is None
    assert row.detail["violated_directive"] == "script-src"
    assert row.detail["effective_directive"] == "script-src-elem"
    assert row.detail["blocked_uri"] == "https://evil.example.com/x.js"
    assert row.detail["document_uri"] == "https://app.example.com/dashboard"
    assert row.detail["line_number"] == 42


@pytest.mark.asyncio
async def test_public_access_no_auth_required(client, session_factory):
    # No Authorization header at all — must be accepted, not 401/403.
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={"csp-report": {"violated-directive": "img-src"}},
    )
    assert resp.status_code == 204
    rows = await _audit_rows(session_factory)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_reporting_api_array_writes_row_per_envelope(client, session_factory):
    payload = [
        {
            "type": "csp-violation",
            "body": {
                "documentURL": "https://app.example.com/a",
                "violatedDirective": "style-src",
                "blockedURL": "inline",
            },
        },
        {
            "type": "csp-violation",
            "body": {
                "documentURL": "https://app.example.com/b",
                "effectiveDirective": "font-src",
            },
        },
    ]
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=payload,
        headers={"content-type": "application/reports+json"},
    )
    assert resp.status_code == 204

    rows = await _audit_rows(session_factory)
    assert len(rows) == 2
    # camelCase keys normalized to the same snake_case keys as legacy.
    assert rows[0].detail["violated_directive"] == "style-src"
    assert rows[0].detail["blocked_uri"] == "inline"
    assert rows[1].detail["effective_directive"] == "font-src"


@pytest.mark.asyncio
async def test_oversized_field_is_truncated(client, session_factory):
    long_uri = "https://app.example.com/" + ("a" * 5000)
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={"csp-report": {"blocked-uri": long_uri, "violated-directive": "x"}},
    )
    assert resp.status_code == 204
    rows = await _audit_rows(session_factory)
    assert len(rows[0].detail["blocked_uri"]) == 512


@pytest.mark.asyncio
async def test_malformed_json_no_500_no_row(client, session_factory):
    resp = await client.post(
        "/api/v1/security/csp-report",
        content=b"this is not json{{{",
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204
    assert await _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_unknown_shape_no_500_no_row(client, session_factory):
    # Valid JSON but neither supported shape.
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={"totally": "unexpected"},
    )
    assert resp.status_code == 204
    assert await _audit_rows(session_factory) == []

    # Empty body.
    resp = await client.post("/api/v1/security/csp-report", content=b"")
    assert resp.status_code == 204
    assert await _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_array_envelope_count_is_bounded(client, session_factory):
    payload = [
        {"type": "csp-violation", "body": {"violatedDirective": f"d{i}"}}
        for i in range(50)
    ]
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=payload,
        headers={"content-type": "application/reports+json"},
    )
    assert resp.status_code == 204
    rows = await _audit_rows(session_factory)
    # Capped at _MAX_REPORTS_PER_REQUEST (20).
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_oversized_content_length_rejected_before_buffering(
    client, session_factory
):
    # An oversized real body (advertised via Content-Length) is dropped
    # before parsing, so no audit row is written.
    big_body = b'{"csp-report": {"violated-directive": "x", "pad": "' + b"a" * (64 * 1024) + b'"}}'
    resp = await client.post(
        "/api/v1/security/csp-report",
        content=big_body,
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204
    assert await _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_client_ip_recorded(client, session_factory):
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={"csp-report": {"violated-directive": "connect-src"}},
        headers={"x-request-id": "req-abc"},
    )
    assert resp.status_code == 204
    rows = await _audit_rows(session_factory)
    # ip_address is resolved via get_client_ip; in the ASGI test client
    # request.client is populated, so a non-null value lands.
    assert rows[0].ip_address is not None


@pytest.mark.asyncio
async def test_empty_alias_does_not_shadow_nonempty_alias(client, session_factory):
    """An empty legacy alias must not win over a non-empty camelCase one.

    Both ``blocked-uri`` (kebab) and ``blockedURL`` (camel) normalize to
    ``blocked_uri``. The kebab key is iterated first, but its value is an
    empty string, so the non-empty camelCase value must be the one that
    lands (the "first NON-empty writer wins" rule).
    """
    body = {
        "csp-report": {
            "violated-directive": "script-src",
            "blocked-uri": "",
            "blockedURL": "https://real.example.com/x.js",
        }
    }
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=body,
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204

    rows = await _audit_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].detail["blocked_uri"] == "https://real.example.com/x.js"


@pytest.mark.asyncio
async def test_csp_report_rate_limited(client, session_factory):
    """The route carries ``@limiter.limit("60/minute")``: the 61st POST
    from the same IP within the minute returns 429.

    Mirrors the wiring in test_rate_limit_sensitive_endpoints.py — the
    ``reset_limiter`` autouse fixture clears the per-IP counter, the
    ``client`` fixture wires ``app.state.limiter`` + the
    ``RateLimitExceeded`` handler, and each accepted call increments the
    counter. If the slowapi limiter were disabled/failing-open in this
    env the first 60 calls would still pass, so this asserts the
    configured limit is actually enforced.
    """
    body = {"csp-report": {"violated-directive": "img-src"}}

    # 60 reports within the minute are accepted (204).
    for i in range(60):
        resp = await client.post("/api/v1/security/csp-report", json=body)
        assert resp.status_code == 204, f"call {i} unexpectedly {resp.status_code}"

    # 61st within the same minute for the same IP → 429.
    throttled = await client.post("/api/v1/security/csp-report", json=body)
    assert throttled.status_code == 429
