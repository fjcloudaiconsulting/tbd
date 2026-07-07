"""Connect-time SSRF guard for org-configured AI provider endpoints.

Org admins control the ``base_url`` of ``ollama`` / ``openai_compatible``
credentials, which the adapters hit server-side (validate probes, chat,
embed, streaming). Save-time literal-IP validation in
``app.schemas.org_ai_credential`` is defense-in-depth only: a DNS name
whose A/AAAA record points at 169.254.169.254 / 127.0.0.1 / RFC1918
sails straight through it. This module is the enforcement point:

- ``check_ip`` is the single canonical denylist (the schema delegates
  its literal checks here so the two layers can never drift).
- ``GuardedTransport`` resolves the hostname at connect time, validates
  EVERY resolved address, and pins the connection to one validated IP
  so the request cannot re-resolve to something else (no rebinding
  window). TLS keeps verifying against the original hostname: the URL
  host is rewritten to the pinned IP while the ``Host`` header and the
  ``sni_hostname`` request extension stay on the DNS name, so SNI and
  certificate hostname verification are unchanged.
- ``guarded_async_client`` builds an ``httpx.AsyncClient`` on that
  transport with ``follow_redirects=False`` — and even if a caller ever
  enabled redirects, each hop re-enters the transport and is
  re-validated.

``allow_private`` (wired to settings.ai_provider_allow_private_networks
for the ollama adapter only) permits loopback + private-range targets
for self-hosted operators; link-local, cloud-metadata, multicast,
unspecified, and reserved addresses stay blocked regardless.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from ipaddress import IPv4Address, IPv6Address

import httpx

# Cloud metadata IPs (AWS / GCP / Azure / DO all converge on the v4
# address; the v6 one is AWS's IMDS address). Blocked even when
# ``allow_private`` is set.
METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})

_DEFAULT_PORTS = {"http": 80, "https": 443}

# User-facing validation_error text for a refused connect. Safe to
# surface (carries no resolved-address detail — the reachability oracle
# stays closed).
BLOCKED_ADDRESS_VALIDATION_ERROR = (
    "base_url points at a blocked network address "
    "(private, loopback, link-local, or metadata)"
)


class BlockedAddressError(httpx.ConnectError):
    """The target host is, or resolves to, a blocked network address.

    Subclasses ``httpx.ConnectError`` so the adapters' existing
    ``except httpx.HTTPError`` error handling contains it, while the
    validate() paths can catch it specifically for a clearer
    ``validation_error`` message.
    """


def ip_literal_or_none(host: str) -> IPv4Address | IPv6Address | None:
    """Return the parsed IP if ``host`` is a literal address (with
    IPv4-mapped IPv6 unwrapped to its IPv4 form), or None for DNS names."""
    if not host:
        return None
    candidate = host.strip("[]")
    # Strip an IPv6 zone/scope id ("fe80::1%eth0").
    candidate = candidate.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return _unwrap_mapped(ip)


def _unwrap_mapped(
    ip: IPv4Address | IPv6Address,
) -> IPv4Address | IPv6Address:
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def check_ip(
    ip: IPv4Address | IPv6Address, *, allow_private: bool = False
) -> None:
    """Raise ``BlockedAddressError`` unless ``ip`` is an acceptable
    egress target.

    Always blocked (even with ``allow_private``): cloud-metadata IPs,
    link-local (v4 169.254/16, v6 fe80::/10), multicast, unspecified,
    and IETF-reserved addresses. IPv4-mapped IPv6 is unwrapped and
    checked as IPv4.

    With ``allow_private=False`` (the default) the address must also be
    globally routable — loopback, RFC1918, ULA fc00::/7, shared
    100.64/10, documentation ranges, etc. are all refused.
    """
    ip = _unwrap_mapped(ip)
    if str(ip) in METADATA_IPS:
        raise BlockedAddressError(
            "blocked address: cloud metadata endpoint"
        )
    # Loopback first: Python 3.12 marks ::1 is_reserved=True, so the
    # reserved check below would otherwise swallow the allow_private
    # loopback carve-out.
    if ip.is_loopback:
        if allow_private:
            return
        raise BlockedAddressError("blocked address: loopback")
    if (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise BlockedAddressError(
            "blocked address: link-local, multicast, unspecified, "
            "or reserved"
        )
    if not allow_private and not ip.is_global:
        raise BlockedAddressError(
            "blocked address: not a public (globally routable) address"
        )


async def resolve_host(
    host: str, port: int
) -> list[IPv4Address | IPv6Address]:
    """Resolve ``host`` to every A/AAAA record via the event loop's
    executor-backed getaddrinfo (async-safe; no blocking DNS on the
    loop)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise httpx.ConnectError(
            f"DNS resolution failed for {host!r}"
        ) from None
    addresses: list[IPv4Address | IPv6Address] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canon, sockaddr in infos:
        raw = str(sockaddr[0]).split("%", 1)[0]
        if raw in seen:
            continue
        seen.add(raw)
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            raise BlockedAddressError(
                f"DNS resolution for {host!r} returned an unparseable "
                "address"
            ) from None
    return addresses


class GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport that validates + pins the target address.

    - Literal-IP hosts are validated directly.
    - DNS hosts are resolved once per (host, port); EVERY record must
      pass ``check_ip``, then the connection is pinned to the first
      validated address by rewriting the request URL to that IP while
      keeping the ``Host`` header and TLS SNI (via the ``sni_hostname``
      request extension) on the original hostname, so HTTPS certificate
      verification still runs against the DNS name.
    - Because every request (including any redirect hop, should a
      caller enable redirects) passes through here, there is no
      re-resolution window after validation.
    """

    def __init__(self, *, allow_private: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._allow_private = allow_private
        self._pinned: dict[tuple[str, int], str] = {}

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        host = request.url.host
        literal = ip_literal_or_none(host)
        if literal is not None:
            check_ip(literal, allow_private=self._allow_private)
            return await super().handle_async_request(request)

        port = request.url.port or _DEFAULT_PORTS.get(
            request.url.scheme, 80
        )
        key = (host, port)
        pinned = self._pinned.get(key)
        if pinned is None:
            addresses = await resolve_host(host, port)
            if not addresses:
                raise httpx.ConnectError(
                    f"DNS resolution for {host!r} returned no addresses"
                )
            for address in addresses:
                check_ip(address, allow_private=self._allow_private)
            pinned = str(addresses[0])
            self._pinned[key] = pinned

        # Preserve the original Host header (host[:port]) and keep TLS
        # SNI + certificate verification on the DNS name while the TCP
        # connection goes to the pinned, validated IP.
        host_header = request.headers.get("Host")
        request.extensions = dict(request.extensions)
        request.extensions["sni_hostname"] = host
        request.url = request.url.copy_with(host=pinned)
        if host_header:
            request.headers["Host"] = host_header
        return await super().handle_async_request(request)


def guarded_async_client(
    *, timeout: float, allow_private: bool = False
) -> httpx.AsyncClient:
    """AsyncClient factory for org-configured AI endpoints: pinned
    transport + no redirect following."""
    return httpx.AsyncClient(
        timeout=timeout,
        transport=GuardedTransport(allow_private=allow_private),
        follow_redirects=False,
    )
