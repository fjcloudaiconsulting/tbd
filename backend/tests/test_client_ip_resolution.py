"""Regression tests for ``get_client_ip`` (audit log IP bug fix).

Reported 2026-05-12: ``audit_events.ip_address`` was recording the
Docker-bridge IP in local dev and the DO App Platform ingress IP in
prod instead of the real client IP. Root cause: ``get_client_ip``
trusted ``request.client.host`` (the post-uvicorn-XFF value) as the
final answer instead of parsing ``X-Forwarded-For`` left-to-right
and returning the first non-trusted-proxy entry. The result was
that any chain where every entry fell inside ``_TRUSTED_PROXY_CIDRS``
collapsed to the leftmost private IP (dev), or any chain where the
ingress peer was outside the trust list skipped ``do-connecting-ip``
entirely (prod).

These tests pin the five scenarios from the bug memo. They use
synthetic ``Request`` objects so they don't depend on uvicorn at all
- the helper must be correct regardless of how the upstream stack
populates ``request.client.host``.
"""
from __future__ import annotations

from typing import Optional

from starlette.requests import Request

from app.rate_limit import get_client_ip


def _make_request(
    *,
    client_host: Optional[str],
    headers: Optional[dict[str, str]] = None,
) -> Request:
    """Build a minimal Starlette Request whose ``client.host`` and
    headers match the post-uvicorn-XFF-processing state the audit
    callers see.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))

    scope: dict = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": (client_host, 0) if client_host else None,
        "server": ("testserver", 80),
    }
    return Request(scope)


# ── Scenario 1: direct browser localhost ──────────────────────────────────


def test_direct_browser_localhost_returns_loopback():
    """No proxy, no XFF header, direct TCP peer is loopback."""
    request = _make_request(client_host="127.0.0.1")
    assert get_client_ip(request) == "127.0.0.1"


# ── Scenario 2: single trusted proxy, real client in XFF ──────────────────


def test_single_trusted_proxy_returns_real_client_from_xff():
    """nginx in front of backend (dev / on-prem). XFF carries the real
    browser IP; the immediate TCP peer is the nginx container's
    private bridge IP. We must return the browser IP, not the proxy.
    """
    request = _make_request(
        client_host="172.18.0.5",
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert get_client_ip(request) == "203.0.113.7"


# ── Scenario 3: two trusted proxies, client at the head of the chain ──────


def test_two_trusted_proxies_returns_first_non_proxy_entry():
    """Two-hop proxy chain (e.g. nginx in front of an internal LB).
    XFF is ``<client>, <hop1>, <hop2>``. We pick the leftmost
    non-trusted entry, which is the real client.
    """
    request = _make_request(
        client_host="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.5"},
    )
    assert get_client_ip(request) == "203.0.113.7"


# ── Scenario 4: DO App Platform — do-connecting-ip is authoritative ──────


def test_do_app_platform_uses_do_connecting_ip_when_peer_is_trusted():
    """DO App Platform: ingress sets ``do-connecting-ip`` with the
    real client and sets XFF to the DO ingress IP itself. The direct
    TCP peer is in our trust list, so the header is authoritative.
    """
    request = _make_request(
        client_host="10.0.0.5",
        headers={
            "X-Forwarded-For": "10.244.0.3",
            "do-connecting-ip": "203.0.113.7",
        },
    )
    assert get_client_ip(request) == "203.0.113.7"


# ── Scenario 5: forged do-connecting-ip from a public peer is REJECTED ────


def test_untrusted_public_peer_with_do_connecting_ip_returns_peer():
    """A caller reaching the backend on a public network path tries
    to forge ``do-connecting-ip`` to claim a different source IP. We
    must refuse to trust the header and return the public peer.
    """
    request = _make_request(
        client_host="198.51.100.99",
        headers={"do-connecting-ip": "203.0.113.7"},
    )
    assert get_client_ip(request) == "198.51.100.99"


# ── Additional spoof-resistance: forged left-hand XFF entry ──────────────


def test_forged_leftmost_xff_from_untrusted_peer_is_rejected():
    """A direct-from-internet caller spoofs an XFF header claiming
    to be ``203.0.113.7``. The TCP peer is a public IP we don't
    trust as a proxy, so we ignore XFF entirely and return the peer.
    """
    request = _make_request(
        client_host="198.51.100.99",
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert get_client_ip(request) == "198.51.100.99"


# ── Edge case: missing client (test stack, no TCP peer) ──────────────────


def test_missing_client_falls_back_to_loopback():
    """Some test scopes don't set ``client`` at all. The helper must
    not crash; return the loopback sentinel.
    """
    request = _make_request(client_host=None)
    assert get_client_ip(request) == "127.0.0.1"


# ── Edge case: XFF with whitespace and IPv6 ───────────────────────────────


def test_xff_handles_whitespace_around_entries():
    """nginx appends with ``, `` so entries have leading whitespace.
    We must strip it before checking trust / returning.
    """
    request = _make_request(
        client_host="172.18.0.5",
        headers={"X-Forwarded-For": "  203.0.113.7  ,   172.18.0.5  "},
    )
    assert get_client_ip(request) == "203.0.113.7"


def test_ipv6_client_in_xff_is_returned():
    """IPv6 clients reach the proxy and end up at the head of XFF."""
    request = _make_request(
        client_host="172.18.0.5",
        headers={"X-Forwarded-For": "2001:db8::1, 172.18.0.5"},
    )
    assert get_client_ip(request) == "2001:db8::1"


# ── Regression: chain entirely inside trusted CIDRs ──────────────────────


def test_xff_entirely_trusted_falls_back_to_do_or_peer():
    """All XFF entries are private IPs (the pathological dev case
    that originally surfaced the bug — Docker-bridge-only chain).
    With no ``do-connecting-ip`` we return the direct peer because
    there is genuinely no public client IP to surface.
    """
    request = _make_request(
        client_host="172.18.0.5",
        headers={"X-Forwarded-For": "192.168.65.1, 172.18.0.5"},
    )
    # No public entry in XFF and no DO header — peer is the best
    # answer we have. Crucially we DON'T return one of the trusted
    # CIDR entries as if it were the user.
    assert get_client_ip(request) == "172.18.0.5"


def test_xff_entirely_trusted_with_do_connecting_ip_prefers_do():
    """DO App Platform shape: XFF is private (the DO ingress hop)
    and ``do-connecting-ip`` carries the real client. Must return
    the DO header.
    """
    request = _make_request(
        client_host="10.244.0.3",
        headers={
            "X-Forwarded-For": "10.244.0.3",
            "do-connecting-ip": "203.0.113.7",
        },
    )
    assert get_client_ip(request) == "203.0.113.7"


def test_empty_xff_header_falls_back_to_peer():
    """An XFF header that's literally empty (or just whitespace)
    must not crash and must not be treated as a valid chain.
    """
    request = _make_request(
        client_host="127.0.0.1",
        headers={"X-Forwarded-For": ""},
    )
    assert get_client_ip(request) == "127.0.0.1"


# ── Integration: audit_events.ip_address persists the resolved client ─────


import datetime as _dt
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event as _sa_event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def _audit_session_factory():
    from app.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @_sa_event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_endpoint_persists_real_client_ip_from_xff(
    _audit_session_factory,
):
    """End-to-end pin: hit an audited endpoint with an XFF chain
    whose leftmost entry is a public IP and whose direct TCP peer is
    a trusted proxy. Assert the persisted ``audit_events.ip_address``
    equals the leftmost (real) client IP, not the proxy peer.

    Before the fix this column captured the TestClient peer
    ``127.0.0.1`` (analogue of the Docker bridge / DO ingress IP the
    user reported). After the fix it captures the leftmost
    non-trusted XFF entry — the real client.
    """
    from app.database import get_db
    from app.deps import get_current_user, get_session_factory
    from app.models.audit_event import AuditEvent
    from app.models.subscription import (
        BillingInterval,
        Plan,
        Subscription,
        SubscriptionStatus,
    )
    from app.models.user import Organization, Role, User
    from app.routers.admin_orgs import router as admin_orgs_router
    from app.security import hash_password

    # Seed: superadmin + target org with subscription.
    async with _audit_session_factory() as db:
        plan = Plan(slug="free", name="Free")
        db.add(plan)
        admin_org = Organization(name="Admin Org", billing_cycle_day=1)
        target = Organization(name="Target Inc", billing_cycle_day=1)
        db.add_all([admin_org, target])
        await db.commit()
        sa = User(
            org_id=admin_org.id,
            username="root",
            email="root@platform.io",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            is_superadmin=True,
            is_active=True,
            email_verified=True,
        )
        db.add(sa)
        await db.commit()
        admin_user_id = sa.id
        target_id = target.id
        db.add_all([
            Subscription(
                org_id=target.id,
                plan_id=plan.id,
                status=SubscriptionStatus.TRIALING,
                billing_interval=BillingInterval.MONTHLY,
                trial_end=_dt.date.today() + _dt.timedelta(days=14),
            ),
            Subscription(
                org_id=admin_org.id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                billing_interval=BillingInterval.MONTHLY,
            ),
        ])
        await db.commit()

    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with _audit_session_factory() as session:
            yield session

    async def override_current_user() -> User:
        async with _audit_session_factory() as db:
            return (
                await db.execute(select(User).where(User.id == admin_user_id))
            ).scalar_one()

    def override_session_factory():
        return _audit_session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[get_session_factory] = override_session_factory
    app.include_router(admin_orgs_router)

    # TestClient's default ASGI scope sets ``client`` to
    # ``("testclient", 50000)``, which is not in our trust CIDRs and
    # would cause ``get_client_ip`` to ignore XFF. Wrap the app in a
    # thin ASGI middleware that rewrites ``client`` to a loopback
    # address — mimicking the post-uvicorn scope when the immediate
    # TCP peer is a trusted proxy (nginx in dev, DO ingress in prod).
    real_app = app

    async def trusted_peer_wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("127.0.0.1", 50000)}
        await real_app(scope, receive, send)

    with TestClient(trusted_peer_wrapper) as client:
        res = client.put(
            f"/api/v1/admin/orgs/{target_id}/subscription",
            json={"status": "active"},
            headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.5"},
        )
    assert res.status_code == 200, res.text

    async with _audit_session_factory() as db:
        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "admin.org.subscription.override"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].ip_address == "203.0.113.7"
