"""TBD-634: Client ID Metadata Documents (CIMD) for the MCP OAuth server.

The document fetch is faked with ``httpx.MockTransport`` injected by
monkeypatching ``oauth_service.guarded_async_client``, except where a test
names the real guarded client. Fixtures and helpers come from ``test_oauth``.
Each docstring names the wrong implementation the fence kills.
"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import func, select

from app.models.oauth_client import OAuthClient
from app.services import oauth_service
from app.services.ai_providers import egress_guard
from app.services.oauth_service import CimdError, cimd_id, cimd_key, fetch_cimd
from tests.agent.test_agent_tokens import _jwt, _org, _user
from tests.agent.test_oauth import (  # noqa: F401 -- fixtures + helpers
    CB, CTX, RESOURCE, _consent, _env, _err, _exchange, _grant, _params, _pkce, _q,
    _refresh, _register, client, factory,
)

URL = "https://claude.ai/oauth/mcp-oauth-client-metadata"
BAD = {"detail": {"code": "invalid_client"}}


def _doc(url=URL, uris=(CB,), name="Claude Code", **extra) -> dict:
    return {"client_id": url, "client_name": name, "redirect_uris": list(uris),
            "token_endpoint_auth_method": "none", **extra}


def _resp(status=200, body=b"", headers=None, stream=None) -> httpx.Response:
    # ``content=`` would pre-read the body, and aiter_raw would then raise StreamConsumed.
    return httpx.Response(status, headers={"Content-Type": "application/json", **(headers or {})},
                          stream=stream or httpx.ByteStream(body))


class Web:
    """The fake internet: url -> Response (or a callable); records requests."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def serve(self, url, doc=None, *, status=200, headers=None, content=None, stream=None):
        body = json.dumps(doc).encode() if content is None else content
        self.routes[url] = lambda: _resp(status, body, headers, stream)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        route = self.routes.get(str(request.url))
        return route() if route else httpx.Response(404)


@pytest.fixture
def web(monkeypatch):
    w = Web()

    def fake_client(*, timeout, allow_private=False, executor=None):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(w.handle),
                                 follow_redirects=False)

    monkeypatch.setattr(oauth_service, "guarded_async_client", fake_client)
    w.serve(URL, _doc())
    return w


class Chunks(httpx.AsyncByteStream):
    def __init__(self, n, size=1024, body=None):
        self.n, self.size, self.body, self.yielded = n, size, body, 0

    async def __aiter__(self):
        for _ in range(self.n):
            self.yielded += 1
            yield self.body or b" " * self.size


class Slow(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        await asyncio.sleep(1)
        yield self.body


async def _login(factory):
    uid = await _user(factory, await _org(factory, "A"), "m")
    return uid, await _jwt(factory, uid)


async def _ctx(client, h, cid=URL, redirect=CB):
    return await client.get(CTX, params=_params(cid, _pkce()[1], redirect=redirect), headers=h)


async def _refused(reason=None, url=URL):
    with pytest.raises(CimdError) as ei:
        await fetch_cimd(url)
    if reason:
        assert ei.value.reason == reason
    return ei.value


async def _rows(factory):
    async with factory() as s:
        return list((await s.execute(select(OAuthClient))).scalars().all())


# ── fetch_cimd: transport and parsing (1-9) ─────────────────────────────────


async def test_fetch_returns_name_and_uris(web):
    assert await fetch_cimd(URL) == ("Claude Code", [CB])


@pytest.mark.parametrize("ip", ["10.0.0.1", "169.254.169.254"])
async def test_1_private_ip_refused(monkeypatch, ip):
    """FENCE 1. Wrong implementation: a plain httpx client, or allow_private=True."""
    seen = []

    async def fake_resolve(host, port, executor=None):
        seen.append(host)
        return [ipaddress.ip_address(ip)]

    monkeypatch.setattr(egress_guard, "resolve_host", fake_resolve)
    monkeypatch.setattr(oauth_service, "CIMD_TIMEOUT_S", 1.0)
    await _refused("BlockedAddressError")
    assert seen == ["claude.ai"]


@pytest.mark.parametrize("url", [
    "https://claude.ai:8443/x", "https://user@claude.ai/x", "https://CLAUDE.AI/x",
    "https://claude.ai./x", "https://1.2.3.4/x", "https://[::1]/x", "https://claude.ai/x?a=b",
    "https://claude.ai/a/./b", "https://claude.ai/a/../b", "https://claude.ai/",
    "https://claude.ai", "https://localhost/x", "https://claude.ai/x%2e", "https://claude.ai/x\\y",
    "https://claude.ai/x y", "https://claude.ai/x#f",
])
async def test_2_url_shape_refused_before_io(factory, client, web, url):
    """FENCE 2. Wrong implementation: the URL checks after the fetch, or a port allowed."""
    web.serve(url, _doc(url))
    _, h = await _login(factory)
    r = await _ctx(client, h, url)
    assert (r.status_code, r.json()) == (400, BAD)
    assert web.calls == []


async def test_3_redirect_refused(web):
    """FENCE 3. Wrong implementation: follow_redirects=True (or a content-type-only check)."""
    web.serve(URL, status=302, headers={"Location": "https://evil.example/doc"}, content=b"{}")
    web.serve("https://evil.example/doc", _doc(URL))
    await _refused()
    assert len(web.calls) == 1


async def test_4_oversized_refused(web):
    """FENCE 4. Wrong implementation: aread() then a length check, or trusting Content-Length."""
    big = Chunks(1)
    web.serve(URL, headers={"Content-Length": "6000"}, stream=big)
    await _refused()
    assert big.yielded == 0  # refused before reading
    chunked = Chunks(32)  # 32 KiB, no Content-Length
    web.serve(URL, stream=chunked)
    await _refused()
    assert chunked.yielded <= 6


async def test_5_gzip_refused(web):
    """FENCE 5. Wrong implementation: aiter_bytes with no Content-Encoding check
    (a decoding implementation would accept this valid document)."""
    web.serve(URL, content=gzip.compress(json.dumps(_doc()).encode()),
              headers={"Content-Encoding": "gzip"})
    await _refused()


async def test_6_slow_body_times_out(web, monkeypatch):
    """FENCE 6. Wrong implementation: httpx per-phase timeouts only."""
    monkeypatch.setattr(oauth_service, "CIMD_TIMEOUT_S", 0.2)
    web.serve(URL, stream=Slow(json.dumps(_doc()).encode()))
    await _refused("TimeoutError")


async def test_7_duplicate_key_refused(web):
    """FENCE 7. Wrong implementation: a bare json.loads."""
    for body in (
        f'{{"client_id": "{URL}", "client_id": "{URL}", "redirect_uris": ["{CB}"]}}',
        f'{{"client_id": "{URL}", "redirect_uris": ["{CB}"], "x": {{"a": 1, "a": 2}}}}',
    ):
        web.serve(URL, content=body.encode())
        await _refused()


@pytest.mark.parametrize("cid", [URL + "/", "https://claude.ai/other", None, 7])
async def test_8_client_id_mismatch_refused(web, cid):
    """FENCE 8. Wrong implementation: no self-check, or a normalised one."""
    web.serve(URL, _doc(cid))
    await _refused()


@pytest.mark.parametrize("extra", [
    {"client_secret": "s"}, {"token_endpoint_auth_method": "client_secret_basic"},
])
async def test_9_secret_or_auth_method_refused(web, extra):
    """FENCE 9. Wrong implementation: extra fields ignored."""
    web.serve(URL, _doc(**extra))
    await _refused()


async def test_9_extra_fields_ignored_and_method_optional(web):
    d = _doc(grant_types=["urn:ietf:params:oauth:grant-type:jwt-bearer"], x={"a": 1})
    del d["token_endpoint_auth_method"]
    web.serve(URL, d)
    assert await fetch_cimd(URL) == ("Claude Code", [CB])


# ── verified (10) ───────────────────────────────────────────────────────────


async def test_10_verified_matrix(factory, client, web):
    """FENCE 10. Wrong implementation: host-based (endswith, registrable
    domain, allowlisted host) or row- or name-derived verified."""
    _, h = await _login(factory)
    other = "https://claude.ai/other/path"
    sub = "https://sub.claude.ai/oauth/mcp-oauth-client-metadata"
    ext = "https://example.com/oauth/doc"
    web.serve(URL, _doc(uris=[CB, "https://chatgpt.com/cb", "http://127.0.0.1/cb"]))
    web.serve(other, _doc(other))
    web.serve(sub, _doc(sub, uris=["https://sub.claude.ai/cb"]))
    web.serve(ext, _doc(ext, uris=["https://example.com/cb"]))
    dcr = await _register(client, name="Claude")
    cases = [
        (URL, CB, True), (URL, "https://chatgpt.com/cb", False), (URL, "http://127.0.0.1:9/cb", False),
        (other, CB, False), (sub, "https://sub.claude.ai/cb", False),
        (ext, "https://example.com/cb", False), (dcr.json()["client_id"], CB, False),
    ]
    web.serve(other, _doc(other))
    for cid, redirect, want in cases:
        r = await _ctx(client, h, cid, redirect)
        assert r.status_code == 200, (cid, r.text)
        assert r.json()["client_name_verified"] is want, (cid, redirect)
        assert r.json()["client_id"] == cid


# ── rows (11-15) ────────────────────────────────────────────────────────────


async def test_11_context_and_deny_create_no_row(factory, client, web):
    """FENCE 11. Wrong implementation: the upsert inside validate_consent."""
    _, h = await _login(factory)
    assert (await _ctx(client, h)).status_code == 200
    r, _ = await _consent(client, h, URL, approve=False)
    assert r.status_code == 200 and _q(r.json()["redirect_to"])["error"] == "access_denied"
    assert await _rows(factory) == []


async def test_12_approve_creates_row_with_cimd_id_and_grant(factory, client, web):
    uid, h = await _login(factory)
    r, _ = await _consent(client, h, URL)
    assert r.status_code == 200, r.text
    [row] = await _rows(factory)
    assert row.id == cimd_id(URL) and row.metadata_key == cimd_key(row.id)
    assert (row.client_name, row.redirect_uris) == ("Claude Code", [CB])
    assert row.last_used_at is not None


async def test_13_doc_change_updates_row_same_id(factory, client, web):
    """FENCE 13. Wrong implementation: insert-only (stale), or a new row per change."""
    _, h = await _login(factory)
    assert (await _consent(client, h, URL))[0].status_code == 200
    cb2 = "https://claude.ai/api/mcp/auth_callback2"
    web.serve(URL, _doc(name="Renamed", uris=[CB, cb2]))
    assert (await _consent(client, h, URL, redirect=cb2))[0].status_code == 200
    [row] = await _rows(factory)
    assert (row.id, row.client_name, row.redirect_uris) == (cimd_id(URL), "Renamed", [CB, cb2])


async def test_14_hex_id_of_cimd_row_refused(factory, client, web):
    """FENCE 14. Wrong implementation: the DCR path serving CIMD rows (no R5 refusal)."""
    _, h = await _login(factory)
    assert (await _consent(client, h, URL))[0].status_code == 200
    n = len(web.calls)
    r = await _ctx(client, h, cimd_id(URL))
    assert (r.status_code, r.json()) == (400, BAD)
    assert len(web.calls) == n


async def test_15_key_disjoint_from_dcr(factory, client, web):
    """FENCE 15. Wrong implementation: metadata_key(name, uris) for CIMD rows."""
    _, h = await _login(factory)
    assert (await _consent(client, h, URL))[0].status_code == 200
    r = await _register(client, [CB], name="Claude Code")
    assert r.status_code == 201 and r.json()["client_id"] != cimd_id(URL)
    assert len(await _rows(factory)) == 2


# ── token endpoint (16, 17) ─────────────────────────────────────────────────


async def test_16_exchange_and_refresh_accept_url_client_id(factory, client, web):
    """FENCE 16. Wrong implementation: raw == against the stored hex id."""
    g = await _grant(factory, client, cid=URL)
    assert _err(await _exchange(client, g, client_id=URL + "x")) == (400, "invalid_grant")
    assert _err(await _exchange(client, g, client_id=cimd_id(URL))) == (400, "invalid_grant")  # F5
    r = await _exchange(client, g)
    assert r.status_code == 200, r.text
    rt = r.json()["refresh_token"]
    assert _err(await _refresh(client, rt, client_id="https://claude.ai/other")) == (400, "invalid_grant")
    assert _err(await _refresh(client, rt, client_id=cimd_id(URL))) == (400, "invalid_grant")
    r = await _refresh(client, rt, client_id=URL)
    assert r.status_code == 200, r.text
    r = await _refresh(client, r.json()["refresh_token"])  # no client_id (G3)
    assert r.status_code == 200, r.text


async def test_17_token_and_refresh_never_fetch(factory, client, web, monkeypatch):
    """FENCE 17. Wrong implementation: a fetch at /token."""
    g = await _grant(factory, client, cid=URL)

    def boom(*a, **k):
        raise AssertionError("fetched at /token")

    monkeypatch.setattr(oauth_service, "fetch_cimd", boom)
    monkeypatch.setattr(oauth_service, "guarded_async_client", boom)
    r = await _exchange(client, g)
    assert r.status_code == 200, r.text
    assert (await _refresh(client, r.json()["refresh_token"], client_id=URL)).status_code == 200


# ── failure shape, limits (18-20) ───────────────────────────────────────────


async def test_18_fetch_failures_share_one_code(factory, client, web):
    """FENCE 18. Wrong implementation: no catch-all / distinguishable failures
    (a reachability oracle)."""
    _, h = await _login(factory)

    def raising(exc):
        def route():
            raise exc
        return route

    cases = {
        "dns": raising(httpx.ConnectError("dns")),
        "blocked": raising(egress_guard.BlockedAddressError("blocked")),
        "404": lambda: _resp(404),
        "json": lambda: _resp(body=b"{"),
        "deep": lambda: _resp(body=b"[" * 3000),
        "length": raising(ValueError("Content-Length")),
    }
    for name, route in cases.items():
        web.routes[URL] = route
        r = await _ctx(client, h)
        assert (r.status_code, r.json()) == (400, BAD), name


async def test_19_fetch_bucket_429(factory, client, web):
    """FENCE 19. Wrong implementation: unmetered fetches."""
    _, h = await _login(factory)
    for _ in range(30):
        assert (await _ctx(client, h)).status_code == 200
    r = await _ctx(client, h)
    assert (r.status_code, r.json()) == (429, {"detail": {"code": "rate_limited"}})
    assert len(web.calls) == 30


async def test_20_pool_check_on_new_rows_only(factory, client, web, monkeypatch):
    """FENCE 20. Wrong implementation: a pool check on updates, or none on inserts."""
    _, h = await _login(factory)
    await _register(client, name="existing")
    monkeypatch.setattr(oauth_service, "MAX_CLIENTS", 1)
    r, _ = await _consent(client, h, URL)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
    assert len(await _rows(factory)) == 1
    monkeypatch.setattr(oauth_service, "MAX_CLIENTS", 2)
    assert (await _consent(client, h, URL))[0].status_code == 200
    monkeypatch.setattr(oauth_service, "MAX_CLIENTS", 1)  # now full: an update still passes
    assert (await _consent(client, h, URL))[0].status_code == 200


# ── discovery (21) ──────────────────────────────────────────────────────────


async def test_21_as_metadata_advertises_cimd(client):
    r = await client.get("/.well-known/oauth-authorization-server")
    assert r.json()["client_id_metadata_document_supported"] is True


# ── DNS isolation (23) and the concurrent first approval (24) ───────────────


async def test_23_hung_dns_does_not_starve_other_pools(monkeypatch):
    """FENCE 23. Wrong implementation: resolution on the default executor, or on
    the shared egress pool that AI providers use."""
    release = threading.Event()

    def fake_getaddrinfo(host, *a, **k):
        if host == "claude.ai":
            release.wait(10)
            raise socket.gaierror("hung")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(oauth_service, "CIMD_TIMEOUT_S", 0.2)
    pool = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(pool)
    try:
        out = await asyncio.gather(*[fetch_cimd(URL) for _ in range(8)], return_exceptions=True)
        assert all(isinstance(e, CimdError) for e in out), out
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 1) == 1
        assert await asyncio.wait_for(egress_guard.resolve_host("provider.example", 443), 1)
    finally:
        release.set()
        pool.shutdown(wait=False)


async def test_24_concurrent_first_approval_upsert(factory, client, web, monkeypatch):
    """FENCE 24. Wrong implementation: swallowing the IntegrityError without a
    second UPDATE (the row keeps the other writer's stale name)."""
    _, h = await _login(factory)
    real, fired = oauth_service._bucket, []

    async def hooked(key, limit, window):
        if key.startswith("oauth:cimd:host:") and not fired:
            fired.append(1)  # between the UPDATE (rowcount 0) and the INSERT
            async with factory() as other:
                other.add(OAuthClient(id=cimd_id(URL), client_name="stale", redirect_uris=[CB],
                                      metadata_key=cimd_key(cimd_id(URL))))
                await other.commit()
        return await real(key, limit, window)

    monkeypatch.setattr(oauth_service, "_bucket", hooked)
    r, _ = await _consent(client, h, URL)
    assert r.status_code == 200, r.text
    assert fired
    [row] = await _rows(factory)
    assert row.client_name == "Claude Code"
