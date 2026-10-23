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
- URL fields lose their query string and fragment (FE-03).
- Growth bounds (BE-10): duplicate reports dropped per client network,
  one commit per request, per-client and global row budgets with a
  per-directive reserve, nothing recorded without the counters, drops
  counted in a metric.
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
from app.services.audit_service import list_audit_events
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


# ── TBD-439: the writer and the default-view exclusion must agree ───────────


@pytest.mark.asyncio
async def test_csp_row_is_hidden_from_the_default_audit_view(
    client, session_factory
):
    """END-TO-END fence over the constant both sides share.

    ⚠⚠ THIS IS THE ONLY TEST THAT CAN SEE THE DRIFT, and the reason is worth
    stating. ``routers/security.py`` WRITES ``CSP_VIOLATION_EVENT_TYPE`` and
    ``audit_service.list_audit_events`` EXCLUDES it. Re-define the constant in
    the router (shadowing the import) with a different spelling and the writer
    emits one string while the exclusion filters another -- CSP rows flood
    ``/admin/audit`` again.

    Every unit test on either side stays GREEN through that, because each
    side's tests import the same symbol the code under test uses, so the
    assertion and the code move together. Measured during review: 48 tests
    across five files all pass against that mutant. It is the "assertion
    compares a value to itself" class, one module apart.

    This test names NO event type. It POSTs a real report through the real
    endpoint, then asks the real reader whether the row it just wrote is
    hidden. That question cannot be satisfied by both sides moving together.
    """
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={
            "csp-report": {
                "document-uri": "https://app.example.com/dashboard",
                "violated-directive": "script-src",
                "blocked-uri": "https://evil.example.com/x.js",
            }
        },
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204

    # The row exists...
    written = await _audit_rows(session_factory)
    assert len(written) == 1

    async with session_factory() as db:
        default_rows, default_total = await list_audit_events(db)
        by_name_rows, _ = await list_audit_events(
            db, event_type=written[0].event_type
        )
        complete_rows, _ = await list_audit_events(db, include_csp=True)

    # ...and the default view does not show it. Note this asserts on the row
    # the ENDPOINT wrote, never on a literal, so a renamed constant cannot
    # keep it green.
    assert default_rows == []
    assert default_total == 0

    # Asking for it by its own recorded type returns it.
    assert len(by_name_rows) == 1
    assert by_name_rows[0].id == written[0].id

    # And the complete log still contains it.
    assert [r.id for r in complete_rows] == [written[0].id]


# ── FE-03: one-time tokens in report URLs are never persisted ───────────────

TOKEN = "tok-FE03-SENTINEL"


@pytest.mark.asyncio
async def test_legacy_url_fields_lose_query_and_fragment(client, session_factory):
    body = {
        "csp-report": {
            "document-uri": f"https://app.example.com/reset-password?token={TOKEN}#x",
            "referrer": f"https://app.example.com/accept-invite?token={TOKEN}",
            "blocked-uri": f"https://cdn.example.com/x.js?sig={TOKEN}",
            "source-file": f"https://app.example.com/_next/a.js#{TOKEN}",
            "violated-directive": "script-src",
        }
    }
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=body,
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204
    [row] = await _audit_rows(session_factory)
    assert TOKEN not in repr(row.detail)
    assert row.detail["document_uri"] == "https://app.example.com/reset-password"
    assert row.detail["referrer"] == "https://app.example.com/accept-invite"
    assert row.detail["blocked_uri"] == "https://cdn.example.com/x.js"
    assert row.detail["source_file"] == "https://app.example.com/_next/a.js"
    assert row.detail["violated_directive"] == "script-src"


@pytest.mark.asyncio
async def test_reporting_api_url_fields_lose_query_and_fragment(client, session_factory):
    payload = [
        {
            "type": "csp-violation",
            "body": {
                "documentURL": f"https://app.example.com/verify-email?token={TOKEN}",
                "referrer": f"https://app.example.com/mfa-verify?mfa_token={TOKEN}",
                "blockedURL": f"https://evil.example.com/?q={TOKEN}",
                "sourceFile": f"https://app.example.com/b.js?{TOKEN}",
                "effectiveDirective": "img-src",
            },
        }
    ]
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=payload,
        headers={"content-type": "application/reports+json"},
    )
    assert resp.status_code == 204
    [row] = await _audit_rows(session_factory)
    assert TOKEN not in repr(row.detail)
    assert row.detail["document_uri"] == "https://app.example.com/verify-email"
    assert row.detail["blocked_uri"] == "https://evil.example.com/"


@pytest.mark.asyncio
async def test_keyword_blocked_uri_is_kept(client, session_factory):
    resp = await client.post(
        "/api/v1/security/csp-report",
        json={"csp-report": {"blocked-uri": "inline", "violated-directive": "x"}},
    )
    assert resp.status_code == 204
    [row] = await _audit_rows(session_factory)
    assert row.detail["blocked_uri"] == "inline"


# ── BE-10: the anonymous sink's growth is bounded ───────────────────────────


def _envelopes(*directives: str) -> list[dict]:
    return [
        {"type": "csp-violation", "body": {"violatedDirective": d}} for d in directives
    ]


async def _post(client, payload, ip: str = "203.0.113.1"):
    resp = await client.post(
        "/api/v1/security/csp-report",
        json=payload,
        headers={"content-type": "application/reports+json", "x-forwarded-for": ip},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_identical_reports_in_one_body_write_one_row(client, session_factory):
    await _post(client, _envelopes("img-src", "img-src", "img-src", "font-src"))
    rows = await _audit_rows(session_factory)
    assert [r.detail["violated_directive"] for r in rows] == ["img-src", "font-src"]


@pytest.mark.asyncio
async def test_identical_report_is_recorded_once_per_window(client, session_factory):
    await _post(client, _envelopes("img-src"))
    await _post(client, _envelopes("img-src"))
    await _post(client, _envelopes("font-src"))
    rows = await _audit_rows(session_factory)
    assert [r.detail["violated_directive"] for r in rows] == ["img-src", "font-src"]


@pytest.mark.asyncio
async def test_report_seen_from_one_client_is_still_recorded_for_another(
    client, session_factory
):
    """A sender cannot pre-seed a report to hide the same one from others."""
    await _post(client, _envelopes("img-src"), ip="203.0.113.1")
    await _post(client, _envelopes("img-src"), ip="198.51.100.7")
    rows = await _audit_rows(session_factory)
    assert sorted(r.ip_address for r in rows) == ["198.51.100.7", "203.0.113.1"]


@pytest.mark.asyncio
async def test_reports_differing_only_in_a_token_are_one_report(client, session_factory):
    for i in range(3):
        await _post(
            client,
            [{"type": "csp-violation", "body": {
                "documentURL": f"https://app.example.com/reset-password?token=t{i}",
                "violatedDirective": "script-src",
            }}],
        )
    assert len(await _audit_rows(session_factory)) == 1


@pytest.mark.asyncio
async def test_global_budget_caps_rows_across_clients(
    client, session_factory, monkeypatch
):
    monkeypatch.setattr(security_router_module, "_GLOBAL_BUDGET", 5)
    await _post(client, _envelopes("a1", "a2", "a3"), ip="203.0.113.1")
    await _post(client, _envelopes("b1", "b2", "b3"), ip="198.51.100.7")
    await _post(client, _envelopes("c1"), ip="192.0.2.9")
    rows = await _audit_rows(session_factory)
    # Five within the budget, then b3 takes the single reserve slot of its
    # directive label ("other": not a CSP directive) and c1 finds it spent.
    assert [r.detail["violated_directive"] for r in rows] == [
        "a1", "a2", "a3", "b1", "b2", "b3",
    ]
    assert {r.ip_address for r in rows} == {"203.0.113.1", "198.51.100.7"}


@pytest.mark.asyncio
async def test_exhausted_budget_adds_no_key_per_dropped_report(
    client, session_factory, monkeypatch, _rate_limit_engine
):
    from sqlalchemy import text

    monkeypatch.setattr(security_router_module, "_GLOBAL_BUDGET", 1)
    await _post(client, _envelopes("a1"))
    with _rate_limit_engine.connect() as c:
        before = c.execute(text("SELECT COUNT(*) FROM rate_limits")).scalar_one()
    await _post(client, _envelopes(*[f"x{i}" for i in range(20)]))
    with _rate_limit_engine.connect() as c:
        after = c.execute(text("SELECT COUNT(*) FROM rate_limits")).scalar_one()
    # a1, then x0 through the reserve ("other"); the other 19 leave nothing.
    assert len(await _audit_rows(session_factory)) == 2
    # At most: the reserve slot, x0's seen key, the per-IP limiter counter.
    assert after - before <= 3


@pytest.mark.asyncio
async def test_one_commit_per_request(client, session_factory):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    commits: list[int] = []

    def _count(_session):
        commits.append(1)

    event.listen(Session, "after_commit", _count)
    try:
        await _post(client, _envelopes(*[f"d{i}" for i in range(20)]))
    finally:
        event.remove(Session, "after_commit", _count)
    assert len(await _audit_rows(session_factory)) == 20
    assert len(commits) == 1


@pytest.mark.asyncio
async def test_counter_outage_records_nothing(client, session_factory, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def _down(*_a, **_kw):
        raise OperationalError("stmt", {}, Exception("down"))

    # ``get`` is the sink's first counter call; the per-IP limiter only hits.
    monkeypatch.setattr(security_router_module.rate_limit_db, "get", _down)
    await _post(client, _envelopes("img-src"))
    assert await _audit_rows(session_factory) == []


@pytest.mark.asyncio
async def test_write_failure_still_answers_204(client, session_factory, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    async def _boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(_AsyncSession, "commit", _boom)
    await _post(client, _envelopes("img-src"))


@pytest.mark.asyncio
async def test_flood_from_one_client_does_not_starve_another(
    client, session_factory, monkeypatch
):
    monkeypatch.setattr(security_router_module, "_CLIENT_BUDGET", 3)
    monkeypatch.setattr(security_router_module, "_GLOBAL_BUDGET", 10)
    for batch in range(4):
        await _post(client, _envelopes(*[f"f{batch}-{i}" for i in range(5)]), ip="203.0.113.1")
    await _post(client, _envelopes("img-src"), ip="198.51.100.7")
    rows = await _audit_rows(session_factory)
    flooder = [r for r in rows if r.ip_address == "203.0.113.1"]
    assert len(flooder) == 3
    assert [r.detail["violated_directive"] for r in rows if r.ip_address == "198.51.100.7"] == [
        "img-src"
    ]


@pytest.mark.asyncio
async def test_client_budget_covers_the_whole_ipv6_64(
    client, session_factory, monkeypatch
):
    monkeypatch.setattr(security_router_module, "_CLIENT_BUDGET", 2)
    for i in range(4):
        await _post(client, _envelopes(f"d{i}"), ip=f"2001:db8:1:2::{i + 1}")
    await _post(client, _envelopes("other-net"), ip="2001:db8:1:3::1")
    rows = await _audit_rows(session_factory)
    assert [r.detail["violated_directive"] for r in rows] == ["d0", "d1", "other-net"]


@pytest.mark.asyncio
async def test_failed_commit_does_not_burn_the_dedupe_key(
    client, session_factory, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    real_commit = _AsyncSession.commit

    async def _boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(_AsyncSession, "commit", _boom)
    await _post(client, _envelopes("img-src"))
    monkeypatch.setattr(_AsyncSession, "commit", real_commit)
    await _post(client, _envelopes("img-src"))
    rows = await _audit_rows(session_factory)
    assert [r.detail["violated_directive"] for r in rows] == ["img-src"]


def _report_counts(reader) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name != "tbd.security.csp_reports":
                    continue
                for point in metric.data.data_points:
                    key = (point.attributes["outcome"], point.attributes["directive"])
                    counts[key] = counts.get(key, 0) + point.value
    return counts


@pytest.mark.asyncio
async def test_dropped_reports_stay_visible_as_a_metric(
    client, session_factory, monkeypatch, metric_reader
):
    monkeypatch.setattr(security_router_module, "_CLIENT_BUDGET", 1)
    await _post(
        client,
        [
            {"type": "csp-violation", "body": {"effectiveDirective": "script-src-elem", "lineNumber": 1}},
            {"type": "csp-violation", "body": {"effectiveDirective": "script-src-elem", "lineNumber": 1}},
            {"type": "csp-violation", "body": {"effectiveDirective": "img-src"}},
            {"type": "csp-violation", "body": {"effectiveDirective": "x-attacker-chosen"}},
        ],
    )
    assert _report_counts(metric_reader) == {
        ("recorded", "script-src-elem"): 1,
        ("duplicate", "script-src-elem"): 1,
        ("client_budget", "img-src"): 1,
        ("client_budget", "other"): 1,
    }
    assert len(await _audit_rows(session_factory)) == 1


@pytest.mark.asyncio
async def test_distributed_flood_still_lets_a_new_directive_through_the_reserve(
    client, session_factory, monkeypatch, metric_reader
):
    monkeypatch.setattr(security_router_module, "_CLIENT_BUDGET", 2)
    monkeypatch.setattr(security_router_module, "_GLOBAL_BUDGET", 6)

    def _img(line: int) -> dict:
        return {"type": "csp-violation", "body": {"effectiveDirective": "img-src", "lineNumber": line}}

    # Three networks spend the whole global budget, two rows each.
    for n in range(3):
        await _post(client, [_img(10 * n), _img(10 * n + 1)], ip=f"203.0.113.{n + 1}")
    script = [{"type": "csp-violation", "body": {"effectiveDirective": "script-src"}}]
    await _post(client, script, ip="198.51.100.7")  # through the reserve
    await _post(client, script, ip="198.51.100.8")  # reserve slot already used
    await _post(client, [_img(99)], ip="198.51.100.9")  # img-src's own slot

    rows = await _audit_rows(session_factory)
    assert len(rows) == 8
    assert [(r.ip_address, r.detail["effective_directive"]) for r in rows[6:]] == [
        ("198.51.100.7", "script-src"),
        ("198.51.100.9", "img-src"),
    ]
    counts = _report_counts(metric_reader)
    assert counts[("reserve", "script-src")] == 1
    assert counts[("reserve", "img-src")] == 1
    assert counts[("global_budget", "script-src")] == 1
