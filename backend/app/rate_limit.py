"""Rate limiter shared across routers.

The default ``slowapi.util.get_remote_address`` is wrong for this app's
production topology. Two environments exist:

1. **Local / docker-compose — nginx in front of backend.** nginx sets
   ``X-Forwarded-For`` correctly. Uvicorn's ``--proxy-headers`` (trust
   list: RFC 1918 + loopback; see ``backend/Dockerfile``) walks the
   header right-to-left and resolves ``request.client.host`` to the real
   client IP — spoof-resistant because the real upstream appends its
   source to the chain.
2. **Production — DigitalOcean App Platform.** DO's ingress puts the
   real client IP in the custom ``do-connecting-ip`` header and fills
   ``X-Forwarded-For`` with the DO ingress server's own IP. Uvicorn's
   XFF parsing alone therefore resolves ``request.client.host`` to a DO
   ingress IP, not the client — leaving the rate limiter effectively
   global per ingress server. Docs:
   https://docs.digitalocean.com/support/where-can-i-find-the-client-ip-address-of-a-request-connecting-to-my-app/

The keying function below handles both. It consults ``do-connecting-ip``
only when the direct TCP source is a trusted private IP (we're actually
behind an upstream we control), so a caller that manages to hit the
backend directly with a public source IP can't forge the header to
bypass rate limits.
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


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP for rate-limiting purposes.

    On DO App Platform: ``request.client.host`` after uvicorn's proxy
    header processing is still the DO ingress IP (private), so we reach
    for ``do-connecting-ip`` which DO populates with the actual client.
    Elsewhere (dev behind nginx): ``do-connecting-ip`` is absent and
    ``request.client.host`` already holds the resolved client IP.

    The ``do-connecting-ip`` lookup is gated on the direct TCP source
    being a trusted private IP to prevent header forgery by callers
    that reach the backend over a public network path.
    """
    client = request.client
    client_host = client.host if client else None

    if _is_trusted_proxy(client_host):
        do_ip = request.headers.get("do-connecting-ip")
        if do_ip:
            return do_ip

    return client_host or "127.0.0.1"


limiter = Limiter(key_func=get_client_ip)
