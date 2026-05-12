"""Rate limiter and client-IP resolver shared across routers.

The default ``slowapi.util.get_remote_address`` is wrong for this app's
production topology. Two environments exist:

1. **Local / docker-compose — nginx in front of backend.** nginx sets
   ``X-Forwarded-For`` correctly (appending the immediate peer). The
   leftmost entry is the real browser IP. Uvicorn's ``--proxy-headers``
   walks XFF right-to-left and rewrites ``request.client.host``, but
   the rewrite is unreliable when every chain entry falls inside the
   trust CIDRs (the dev common case — both browser host and nginx
   peer are private). We therefore parse XFF ourselves left-to-right
   in :func:`get_client_ip` and return the first non-trusted entry.
2. **Production — DigitalOcean App Platform.** DO's ingress puts the
   real client IP in the custom ``do-connecting-ip`` header and fills
   ``X-Forwarded-For`` with the DO ingress server's own IP. We consult
   ``do-connecting-ip`` only when the direct TCP source (or the XFF
   chain tail) is a trusted private IP, so a caller reaching the
   backend directly with a public source IP can't forge the header to
   bypass the resolver. Docs:
   https://docs.digitalocean.com/support/where-can-i-find-the-client-ip-address-of-a-request-connecting-to-my-app/

The original ``get_client_ip`` (PR #82) relied solely on
``request.client.host`` and ``do-connecting-ip``. That broke the audit
log because uvicorn's XFF processing leaves ``request.client.host`` set
to the leftmost private entry whenever every hop is trusted (dev), and
the DO ingress peer fell outside our trust CIDRs in prod (so
``do-connecting-ip`` was never consulted). The current implementation
walks XFF directly, which fixes both.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable

from slowapi import Limiter
from starlette.requests import Request


# Kept in lockstep with ``--forwarded-allow-ips`` in backend/Dockerfile,
# docker-compose.yml, and docker-compose.prod.yml. When that list changes,
# update here too so the rate limiter's trust boundary matches uvicorn's.
_TRUSTED_PROXY_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
    "127.0.0.0/8",
    "::1/128",
)


def _compile_networks(
    cidrs: Iterable[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(cidr) for cidr in cidrs)


_TRUSTED_PROXY_NETWORKS = _compile_networks(_TRUSTED_PROXY_CIDRS)


def _is_trusted_proxy(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETWORKS)


def _parse_xff(xff_header: str | None) -> list[str]:
    """Split an ``X-Forwarded-For`` header into trimmed entries.

    Returns an empty list when the header is absent or contains only
    whitespace. Each entry has leading/trailing whitespace stripped.
    """
    if not xff_header:
        return []
    return [entry.strip() for entry in xff_header.split(",") if entry.strip()]


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP for rate limiting and audit logging.

    Resolution order:

    1. Parse ``X-Forwarded-For`` left-to-right. The leftmost entry
       that is NOT in our trusted-proxy CIDR list is the real client.
       (Trusting the leftmost entry directly would be spoofable, so we
       gate XFF on the direct TCP peer being a trusted proxy first.)
    2. If the chain is empty OR every entry is a trusted proxy IP,
       fall back to ``do-connecting-ip`` — but only when the direct
       TCP peer is a trusted proxy. DO App Platform uses this header.
    3. Otherwise return ``request.client.host`` (the direct TCP peer).
    """
    client = request.client
    client_host = client.host if client else None

    peer_trusted = _is_trusted_proxy(client_host)
    xff_entries = _parse_xff(request.headers.get("x-forwarded-for"))

    # If the immediate peer is a trusted proxy, the XFF chain it
    # produced is also trusted. Walk left-to-right and return the
    # first non-trusted entry — that's the real client.
    if peer_trusted and xff_entries:
        for entry in xff_entries:
            if not _is_trusted_proxy(entry):
                return entry

    # XFF was absent or every entry was a trusted proxy. DO App
    # Platform encodes the real client in ``do-connecting-ip``; trust
    # it only when the direct peer is itself a trusted proxy.
    if peer_trusted:
        do_ip = request.headers.get("do-connecting-ip")
        if do_ip:
            return do_ip

    # Direct public peer (or no client). Return the peer IP and refuse
    # to honour any forwarded-by headers (they could be forged).
    return client_host or "127.0.0.1"


limiter = Limiter(key_func=get_client_ip)
