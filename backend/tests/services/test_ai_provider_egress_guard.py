"""Connect-time SSRF guard for org-configured AI provider endpoints.

The 2026-07-07 security sweep confirmed the save-time literal-IP checks
in ``app.schemas.org_ai_credential`` are bypassable with any DNS name
whose A/AAAA record points at a private / loopback / metadata address.
These tests pin the enforcement point: ``egress_guard.GuardedTransport``
resolves at connect time, validates EVERY record, and pins the
connection to a validated IP while keeping Host + TLS SNI on the
original hostname.

DNS is mocked throughout (``egress_guard.resolve_host``) — no real
lookups — and the inner ``httpx.AsyncHTTPTransport.handle_async_request``
is stubbed so no real sockets are opened.
"""
from __future__ import annotations

import ipaddress

import httpx
import pytest

from app.config import settings
from app.services.ai_providers import egress_guard
from app.services.ai_providers.base import AIProviderError
from app.services.ai_providers.egress_guard import (
    BlockedAddressError,
    GuardedTransport,
    check_ip,
    guarded_async_client,
    ip_literal_or_none,
)
from app.services.ai_providers.ollama import OllamaAdapter
from app.services.ai_providers.openai_compatible import (
    OpenAICompatibleAdapter,
)


# ---------------------------------------------------------------------------
# check_ip — the canonical denylist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked",
    [
        # Loopback v4 + v6.
        "127.0.0.1",
        "127.55.66.77",
        "::1",
        # RFC1918.
        "10.0.0.5",
        "172.16.5.5",
        "192.168.1.1",
        # ULA fc00::/7.
        "fc00::1",
        "fd12:3456::1",
        # Link-local (covers IMDS 169.254.169.254) + IPv6 fe80::/10.
        "169.254.0.1",
        "169.254.169.254",
        "fe80::1",
        # AWS IPv6 metadata.
        "fd00:ec2::254",
        # Multicast / unspecified / reserved.
        "224.0.0.1",
        "0.0.0.0",
        "::",
        "240.0.0.1",
        # IPv4-mapped IPv6 MUST be unwrapped and checked as IPv4.
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        # IPv6 transition-mechanism literals embedding an internal v4.
        "2002:a9fe:a9fe::",  # 6to4 -> 169.254.169.254 (metadata)
        "2002:7f00:1::",     # 6to4 -> 127.0.0.1 (loopback)
        "2001::7f00:1",      # Teredo literal
        # Alibaba Cloud metadata (CGNAT 100.64/10 shared range).
        "100.100.100.200",
    ],
)
def test_check_ip_blocks_non_public(blocked: str) -> None:
    with pytest.raises(BlockedAddressError):
        check_ip(ipaddress.ip_address(blocked))


@pytest.mark.parametrize(
    "allowed",
    [
        "93.184.216.34",
        "8.8.8.8",
        "2606:2800:220:1::1",
        "2001:4860:4860::8888",
    ],
)
def test_check_ip_allows_public(allowed: str) -> None:
    check_ip(ipaddress.ip_address(allowed))  # must not raise


@pytest.mark.parametrize(
    "allowed_private",
    [
        "10.0.0.5",
        "192.168.1.10",
        "192.168.1.163",
        "127.0.0.1",
        "::1",
        "fc00::1",
    ],
)
def test_check_ip_allow_private_permits_private_and_loopback(
    allowed_private: str,
) -> None:
    check_ip(ipaddress.ip_address(allowed_private), allow_private=True)


@pytest.mark.parametrize(
    "still_blocked",
    [
        # Metadata, link-local, multicast, unspecified, reserved stay
        # blocked even for a private-networks-allowed (ollama) target.
        "169.254.169.254",
        "fd00:ec2::254",
        "169.254.1.1",
        "fe80::1",
        "224.0.0.1",
        "0.0.0.0",
        "240.0.0.1",
        "::ffff:169.254.169.254",
        # Defense-in-depth: the escape hatch relaxes RFC1918 + loopback
        # but must NOT open transition-mechanism literals that embed an
        # internal v4, nor alternate-cloud metadata in the CGNAT range.
        "2002:a9fe:a9fe::",  # 6to4 -> 169.254.169.254 (metadata)
        "2002:7f00:1::",     # 6to4 -> 127.0.0.1 (loopback)
        "2001::7f00:1",      # Teredo literal
        "100.100.100.200",   # Alibaba Cloud metadata
    ],
)
def test_check_ip_allow_private_still_blocks_unsafe(
    still_blocked: str,
) -> None:
    with pytest.raises(BlockedAddressError):
        check_ip(
            ipaddress.ip_address(still_blocked), allow_private=True
        )


def test_ip_literal_or_none_unwraps_and_parses() -> None:
    assert ip_literal_or_none("example.com") is None
    assert str(ip_literal_or_none("[::1]")) == "::1"
    assert str(ip_literal_or_none("::ffff:127.0.0.1")) == "127.0.0.1"
    assert str(ip_literal_or_none("fe80::1%eth0")) == "fe80::1"


# ---------------------------------------------------------------------------
# Transport-level: resolve + validate + pin (mocked DNS, stubbed socket)
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch, mapping: dict[str, list[str]]) -> list:
    """Replace egress_guard.resolve_host with a table-driven fake."""
    calls: list[tuple[str, int]] = []

    async def fake_resolve(host: str, port: int):
        calls.append((host, port))
        return [ipaddress.ip_address(a) for a in mapping[host]]

    monkeypatch.setattr(egress_guard, "resolve_host", fake_resolve)
    return calls


def _patch_inner(monkeypatch, handler=None) -> list[httpx.Request]:
    """Stub the base AsyncHTTPTransport so no real connection opens."""
    captured: list[httpx.Request] = []

    async def fake_inner(self, request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", fake_inner
    )
    return captured


async def _send(
    transport: GuardedTransport, url: str
) -> httpx.Response:
    request = httpx.Request("GET", url)
    return await transport.handle_async_request(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        ["10.0.0.5"],                      # private A record
        ["169.254.169.254"],               # metadata A record
        ["127.0.0.1"],                     # loopback A record
        ["::1"],                           # loopback AAAA record
        ["fe80::1"],                       # link-local AAAA record
        ["fd12:3456::1"],                  # ULA AAAA record
        ["::ffff:127.0.0.1"],              # IPv4-mapped loopback
        ["93.184.216.34", "192.168.1.7"],  # ONE private among public
    ],
)
async def test_hostname_resolving_to_blocked_address_is_refused(
    monkeypatch, records: list[str]
) -> None:
    _patch_resolve(monkeypatch, {"llm.evil.example": records})
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport()
    with pytest.raises(BlockedAddressError):
        await _send(transport, "http://llm.evil.example/v1/models")
    assert inner == []  # never reached the wire


@pytest.mark.asyncio
async def test_public_hostname_is_pinned_with_host_and_sni_preserved(
    monkeypatch,
) -> None:
    _patch_resolve(monkeypatch, {"api.example.com": ["93.184.216.34"]})
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport()
    resp = await _send(
        transport, "https://api.example.com:8443/v1/models"
    )
    assert resp.status_code == 200
    (request,) = inner
    # Connection pinned to the validated IP...
    assert request.url.host == "93.184.216.34"
    assert request.url.port == 8443
    # ...while Host header + TLS SNI stay on the hostname so HTTPS
    # certificate verification is unchanged.
    assert request.headers["Host"] == "api.example.com:8443"
    assert request.extensions["sni_hostname"] == "api.example.com"


@pytest.mark.asyncio
async def test_public_ipv6_hostname_pins_bracketed(monkeypatch) -> None:
    _patch_resolve(
        monkeypatch, {"v6.example.com": ["2606:2800:220:1::1"]}
    )
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport()
    resp = await _send(transport, "https://v6.example.com/v1/models")
    assert resp.status_code == 200
    (request,) = inner
    assert request.url.host == "2606:2800:220:1::1"
    assert request.extensions["sni_hostname"] == "v6.example.com"


@pytest.mark.asyncio
async def test_literal_ip_urls_are_validated_without_dns(
    monkeypatch,
) -> None:
    resolve_calls = _patch_resolve(monkeypatch, {})
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport()
    for url in (
        "http://127.0.0.1:11434/api/tags",
        "http://[::1]:8000/api/tags",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/v1/models",
        "http://[::ffff:10.0.0.1]/v1/models",
    ):
        with pytest.raises(BlockedAddressError):
            await _send(transport, url)
    assert resolve_calls == []
    assert inner == []


@pytest.mark.asyncio
async def test_allow_private_permits_private_but_not_metadata(
    monkeypatch,
) -> None:
    _patch_resolve(
        monkeypatch,
        {
            "ollama.internal": ["192.168.1.163"],
            "sneaky.internal": ["169.254.169.254"],
        },
    )
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport(allow_private=True)
    resp = await _send(
        transport, "http://ollama.internal:11434/api/tags"
    )
    assert resp.status_code == 200
    assert inner[-1].url.host == "192.168.1.163"
    with pytest.raises(BlockedAddressError):
        await _send(transport, "http://sneaky.internal/api/tags")


@pytest.mark.asyncio
async def test_pin_is_cached_per_host_no_second_resolution(
    monkeypatch,
) -> None:
    resolve_calls = _patch_resolve(
        monkeypatch, {"api.example.com": ["93.184.216.34"]}
    )
    inner = _patch_inner(monkeypatch)
    transport = GuardedTransport()
    await _send(transport, "https://api.example.com/v1/models")
    await _send(transport, "https://api.example.com/v1/chat/completions")
    assert len(resolve_calls) == 1
    assert [r.url.host for r in inner] == ["93.184.216.34"] * 2


# ---------------------------------------------------------------------------
# Adapter integration — validate() surfaces a clean error, chat() wraps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_validate_hostname_to_private_is_blocked(
    monkeypatch,
) -> None:
    """Flag OFF (default): a DNS name resolving to RFC1918 must fail
    validation with a blocked-address error, not reach the wire."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", False
    )
    _patch_resolve(monkeypatch, {"rebind.evil.example": ["10.0.0.5"]})
    inner = _patch_inner(monkeypatch)
    adapter = OllamaAdapter(
        base_url="http://rebind.evil.example:11434", api_key="unused"
    )
    result = await adapter.validate()
    assert result.ok is False
    assert "blocked network address" in (result.error or "")
    assert inner == []


@pytest.mark.asyncio
async def test_ollama_validate_loopback_literal_blocked_by_default(
    monkeypatch,
) -> None:
    """The old blanket ollama loopback/private exemption is gone: with
    the flag OFF even a literal loopback base_url refuses to connect."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", False
    )
    inner = _patch_inner(monkeypatch)
    adapter = OllamaAdapter(
        base_url="http://127.0.0.1:11434", api_key="unused"
    )
    result = await adapter.validate()
    assert result.ok is False
    assert "blocked network address" in (result.error or "")
    assert inner == []


@pytest.mark.asyncio
async def test_ollama_flag_on_allows_private_hostname(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", True
    )
    _patch_resolve(monkeypatch, {"ollama.internal": ["192.168.1.163"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"models": [{"name": "llama3.2"}]}
        )

    inner = _patch_inner(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="http://ollama.internal:11434", api_key="unused"
    )
    result = await adapter.validate()
    assert result.ok is True
    assert result.discovered_models == ["llama3.2"]
    assert inner[0].url.host == "192.168.1.163"
    assert inner[0].headers["Host"] == "ollama.internal:11434"


@pytest.mark.asyncio
async def test_openai_compatible_strict_even_with_flag_on(
    monkeypatch,
) -> None:
    """AI_PROVIDER_ALLOW_PRIVATE_NETWORKS is an ollama-only escape
    hatch; openai_compatible keeps the full denylist."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", True
    )
    _patch_resolve(monkeypatch, {"vllm.internal": ["10.0.0.5"]})
    inner = _patch_inner(monkeypatch)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test-1234", base_url="http://vllm.internal:8000"
    )
    result = await adapter.validate()
    assert result.ok is False
    assert "blocked network address" in (result.error or "")
    assert inner == []


@pytest.mark.asyncio
async def test_openai_compatible_chat_blocked_address_wraps(
    monkeypatch,
) -> None:
    _patch_resolve(monkeypatch, {"rebind.evil.example": ["127.0.0.1"]})
    _patch_inner(monkeypatch)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test-1234", base_url="https://rebind.evil.example"
    )
    with pytest.raises(AIProviderError) as excinfo:
        await adapter.chat(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    assert excinfo.value.code == "network_BlockedAddressError"


@pytest.mark.asyncio
async def test_redirect_to_metadata_is_not_followed(
    monkeypatch,
) -> None:
    """A public host 302ing toward the metadata service must not be
    followed: the client refuses redirects outright."""
    _patch_resolve(monkeypatch, {"api.example.com": ["93.184.216.34"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "Location": "http://169.254.169.254/latest/meta-data/"
            },
        )

    inner = _patch_inner(monkeypatch, handler)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test-1234", base_url="https://api.example.com"
    )
    result = await adapter.validate()
    assert result.ok is False
    assert len(inner) == 1  # single request; the 302 was not chased


@pytest.mark.asyncio
async def test_guarded_client_does_not_follow_redirects() -> None:
    client = guarded_async_client(timeout=1.0)
    try:
        assert client.follow_redirects is False
        assert isinstance(
            client._transport, GuardedTransport
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_ollama_chat_goes_through_guard(monkeypatch) -> None:
    """The guard covers the dispatch path (chat), not just validate."""
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", False
    )
    _patch_resolve(monkeypatch, {"rebind.evil.example": ["10.0.0.5"]})
    _patch_inner(monkeypatch)
    adapter = OllamaAdapter(
        base_url="http://rebind.evil.example:11434", api_key="unused"
    )
    with pytest.raises(AIProviderError) as excinfo:
        await adapter.chat(
            model="llama3.2",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert excinfo.value.code == "network_BlockedAddressError"


@pytest.mark.asyncio
async def test_ollama_stream_goes_through_guard(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "ai_provider_allow_private_networks", False
    )
    _patch_resolve(monkeypatch, {"rebind.evil.example": ["10.0.0.5"]})
    _patch_inner(monkeypatch)
    adapter = OllamaAdapter(
        base_url="http://rebind.evil.example:11434", api_key="unused"
    )
    with pytest.raises(AIProviderError) as excinfo:
        async for _chunk in adapter.stream(
            model="llama3.2",
            messages=[{"role": "user", "content": "hi"}],
        ):
            pass
    assert excinfo.value.code == "network_BlockedAddressError"


@pytest.mark.asyncio
async def test_public_hostname_ollama_validate_ok(monkeypatch) -> None:
    """Happy path: public DNS resolves public, request succeeds and is
    pinned."""
    _patch_resolve(
        monkeypatch, {"ollama.example.com": ["93.184.216.34"]}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "llama3.1"}]}
        )

    inner = _patch_inner(monkeypatch, handler)
    adapter = OllamaAdapter(
        base_url="https://ollama.example.com", api_key="unused"
    )
    result = await adapter.validate()
    assert result.ok is True
    assert inner[0].url.host == "93.184.216.34"
    assert inner[0].extensions["sni_hostname"] == "ollama.example.com"
