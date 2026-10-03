"""Regression tests for ``get_client_ip`` (audit log IP bug fix).

Reported 2026-05-12: ``audit_events.ip_address`` was recording the
Docker-bridge IP in local dev and the DO App Platform ingress IP in
prod instead of the real client IP. The fix has two layers:

1. ``get_client_ip`` walks ``X-Forwarded-For`` RIGHT-to-LEFT, skipping
   trusted-proxy hops, and returns the first non-trusted entry. The
   walk only runs when the direct TCP peer is itself a trusted proxy.
2. nginx is configured to set ``X-Forwarded-For $remote_addr`` (NOT
   ``$proxy_add_x_forwarded_for``), so client-supplied XFF chains are
   discarded at the edge. Defense-in-depth - even if right-to-left
   had a bug, the chain reaching the backend is always controlled by
   our infrastructure.
3. When ``PFV_RUNTIME=app_platform`` is set, ``do-connecting-ip`` is
   honoured unconditionally (DO ingress is the only writer; the env
   var is set by us, not the request).

These tests pin all three layers. They use synthetic ``Request``
objects so they don't depend on uvicorn at all - the helper must be
correct regardless of how the upstream stack populates
``request.client.host``.
"""
from __future__ import annotations

from typing import Optional

import pytest
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
    XFF is ``<client>, <hop1>``. Walking right-to-left we skip the
    trusted ``<hop1>`` entry and land on the real client.
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


# ── XFF SPOOFING — two-layer defense ─────────────────────────────────────


def test_xff_spoof_attempt_without_nginx_overwrite_documents_attack_surface():
    """Pre-fix shape: nginx used ``$proxy_add_x_forwarded_for`` which
    APPENDED our peer to whatever the client sent. A malicious client
    could send ``XFF: 1.2.3.4``, nginx forwards ``1.2.3.4, <peer>``,
    and a LEFT-to-right walk picks ``1.2.3.4`` (textbook XFF-spoof CVE).

    With right-to-left walk, the chain ``1.2.3.4, <trusted-peer>`` is
    skipped at the trusted entry and lands on ``1.2.3.4`` - the spoof
    still lands because the attacker controls one extra entry to the
    left of our peer. This is why we ALSO sanitize at nginx (next
    test); the right-to-left walk alone is insufficient when the
    edge passes through a user-supplied prefix.

    This test exists to make the regression visible if the nginx
    overwrite is ever reverted: it documents the residual attack
    surface so a future reviewer can't claim the helper alone is
    spoof-proof.
    """
    request = _make_request(
        client_host="192.168.65.1",
        headers={"X-Forwarded-For": "1.2.3.4, 192.168.65.1"},
    )
    # NOT a security guarantee - the production guarantee comes from
    # nginx overwriting XFF to ``$remote_addr`` so this chain shape
    # never reaches the backend in the first place.
    assert get_client_ip(request) == "1.2.3.4"


def test_xff_spoof_attempt_with_nginx_overwrite_is_blocked():
    """Production shape after nginx overwrite: any client-supplied
    XFF is discarded at the edge and replaced with the nginx peer
    (``$remote_addr``). The backend therefore sees a single-entry
    chain whose only IP is the real browser. The walk returns it
    correctly and there is nothing for an attacker to inject into.
    """
    # Real client at 203.0.113.7 tries to spoof by sending
    # ``XFF: 1.2.3.4``. nginx receives the request, IGNORES the
    # client header, and emits ``XFF: 203.0.113.7`` (its $remote_addr).
    # The backend's TCP peer is the nginx container (private IP).
    request = _make_request(
        client_host="172.18.0.5",
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert get_client_ip(request) == "203.0.113.7"


# ── DO App Platform runtime gate ─────────────────────────────────────────


def test_do_runtime_mode_returns_do_connecting_ip_unconditionally(monkeypatch):
    """With ``PFV_RUNTIME=app_platform`` set, ``do-connecting-ip`` is
    authoritative even when the direct TCP peer is OUTSIDE the
    trusted-proxy CIDR list. This is the prod-fix path: the DO
    ingress peer falls outside RFC 1918, so the old trusted-peer
    gate skipped the header and audit logs got the ingress IP.
    """
    monkeypatch.setenv("PFV_RUNTIME", "app_platform")
    # DO ingress peer is NOT in any of our trusted CIDRs.
    request = _make_request(
        client_host="169.254.10.5",
        headers={"do-connecting-ip": "203.0.113.7"},
    )
    assert get_client_ip(request) == "203.0.113.7"


def test_do_runtime_mode_without_header_falls_back_to_peer(monkeypatch):
    """In App Platform mode, if ``do-connecting-ip`` is somehow
    missing (e.g. health check that does not hit the public ingress),
    we fall back to the standard XFF / peer resolution rather than
    crashing.
    """
    monkeypatch.setenv("PFV_RUNTIME", "app_platform")
    request = _make_request(client_host="169.254.10.5")
    assert get_client_ip(request) == "169.254.10.5"


def test_do_runtime_mode_unset_ignores_do_connecting_ip_from_public_peer(monkeypatch):
    """Without ``PFV_RUNTIME=app_platform``, a request from a public
    peer carrying a forged ``do-connecting-ip`` MUST be rejected.
    Returns the peer IP, not the header.
    """
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    request = _make_request(
        client_host="198.51.100.99",
        headers={"do-connecting-ip": "203.0.113.7"},
    )
    assert get_client_ip(request) == "198.51.100.99"


def test_do_runtime_mode_case_insensitive(monkeypatch):
    """``PFV_RUNTIME`` is matched case-insensitively to avoid spec-vs-
    env quirks (DO docs use mixed case in examples).
    """
    monkeypatch.setenv("PFV_RUNTIME", "App_Platform")
    request = _make_request(
        client_host="169.254.10.5",
        headers={"do-connecting-ip": "203.0.113.7"},
    )
    assert get_client_ip(request) == "203.0.113.7"


# ── CLIENT_IP_HEADER (k3s behind Cloudflare, INFRA-83) ───────────────────
#
# Cloudflare -> Traefik -> backend: uvicorn's proxy-headers rewrite leaves a
# Cloudflare edge (or Traefik) address in request.client.host; the visitor is
# in CF-Connecting-IP, which Cloudflare overwrites on every request.


def test_client_ip_header_returns_cloudflare_visitor(monkeypatch):
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.setenv("CLIENT_IP_HEADER", "cf-connecting-ip")
    request = _make_request(
        client_host="172.70.1.1",  # a Cloudflare edge
        headers={
            "cf-connecting-ip": "203.0.113.7",
            "x-forwarded-for": "6.6.6.6, 203.0.113.7, 172.70.1.1",
        },
    )
    assert get_client_ip(request) == "203.0.113.7"


def test_client_ip_header_accepts_ipv6_and_any_name_case(monkeypatch):
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.setenv("CLIENT_IP_HEADER", "CF-Connecting-IP")
    request = _make_request(
        client_host="10.42.0.9", headers={"cf-connecting-ip": "2001:db8::7"}
    )
    assert get_client_ip(request) == "2001:db8::7"


def test_client_ip_header_unset_ignores_forged_cf_connecting_ip(monkeypatch):
    """DigitalOcean and local dev: anyone can send CF-Connecting-IP."""
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    request = _make_request(
        client_host="198.51.100.99", headers={"cf-connecting-ip": "203.0.113.7"}
    )
    assert get_client_ip(request) == "198.51.100.99"


@pytest.mark.parametrize(
    "value",
    ["not-an-ip", "203.0.113.7, 198.51.100.1", "", "   ", "fe80::1%" + "x" * 80],
)
def test_client_ip_header_with_invalid_value_falls_back(monkeypatch, value):
    """A value that is not one IP address is never used as a rate-limit key."""
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.setenv("CLIENT_IP_HEADER", "cf-connecting-ip")
    request = _make_request(
        client_host="198.51.100.99", headers={"cf-connecting-ip": value}
    )
    assert get_client_ip(request) == "198.51.100.99"


def test_client_ip_header_missing_falls_back_to_peer(monkeypatch):
    """In-cluster callers (the frontend's server-side fetches, probes) send no
    CF-Connecting-IP and resolve as before."""
    monkeypatch.delenv("PFV_RUNTIME", raising=False)
    monkeypatch.setenv("CLIENT_IP_HEADER", "cf-connecting-ip")
    request = _make_request(client_host="10.42.0.12")
    assert get_client_ip(request) == "10.42.0.12"


# ── rate_limit_key: IPv6 buckets by /64 (INFRA-83) ───────────────────────
#
# One routed IPv6 /64 is 2^64 addresses; keyed per /128 a visitor rotates
# addresses and never fills a bucket. The limiter key collapses IPv6 to its
# /64; get_client_ip (audit logs) keeps the full address.


def _key_for(ip: str) -> str:
    from app.rate_limit import rate_limit_key

    return rate_limit_key(_make_request(client_host=ip))


def test_rate_limit_key_shares_one_bucket_per_ipv6_64():
    assert _key_for("2001:db8:1:2::1") == _key_for("2001:db8:1:2:ffff:ffff:ffff:ffff")


def test_rate_limit_key_separates_different_ipv6_64s():
    assert _key_for("2001:db8:1:2::1") != _key_for("2001:db8:1:3::1")


def test_rate_limit_key_leaves_ipv4_alone():
    assert _key_for("203.0.113.7") == "203.0.113.7"
    assert _key_for("203.0.113.7") != _key_for("203.0.113.8")


def test_rate_limit_key_keeps_ipv4_mapped_addresses_apart():
    """Every ::ffff:a.b.c.d sits in ::/64; bucketing them by /64 would put
    all of them in one bucket."""
    assert _key_for("::ffff:203.0.113.7") == "203.0.113.7"
    assert _key_for("::ffff:203.0.113.7") != _key_for("::ffff:203.0.113.8")


def test_rate_limit_key_passes_non_ip_values_through():
    assert _key_for("testclient") == "testclient"


def test_audit_ip_keeps_the_full_ipv6_address():
    request = _make_request(client_host="2001:db8:1:2::1")
    assert get_client_ip(request) == "2001:db8:1:2::1"


def test_limiter_keys_on_rate_limit_key():
    from app import rate_limit

    assert rate_limit.limiter._key_func is rate_limit.rate_limit_key


def test_mcp_auth_failure_counter_keys_on_the_64(monkeypatch):
    import asyncio

    from app import mcp_main

    seen: list[str] = []

    async def _tripped(ip, *, add):
        seen.append(ip)
        return mcp_main.IP_AUTH_FAILURES_PER_MIN

    monkeypatch.setattr(mcp_main, "_ip_failures", _tripped)
    response = asyncio.run(
        mcp_main.mcp_endpoint(_make_request(client_host="2001:db8:1:2::1"))
    )
    assert response.status_code == 429
    assert seen == [_key_for("2001:db8:1:2::2")]


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
