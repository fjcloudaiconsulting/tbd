"""Rate limiter and client-IP resolver shared across routers.

The default ``slowapi.util.get_remote_address`` is wrong for this app's
production topology. Two environments exist:

1. **Local / docker-compose - nginx in front of backend.** nginx sets
   ``X-Forwarded-For $remote_addr`` (the immediate peer ONLY, NOT
   ``$proxy_add_x_forwarded_for``). The chain backend sees is therefore
   always controlled by our infrastructure - client-supplied XFF
   chains are discarded at the edge. The backend resolves the real
   client IP by walking the (sanitized) chain right-to-left, skipping
   trusted-proxy hops, and returning the first non-trusted entry.
2. **Production - DigitalOcean App Platform.** DO's ingress puts the
   real client IP in the custom ``do-connecting-ip`` header and fills
   ``X-Forwarded-For`` with the DO ingress server's own IP. The DO
   ingress peer falls OUTSIDE our private-CIDR trust list (DO uses
   its own runtime networking), so an XFF-only resolver cannot trust
   anything in prod. When ``PFV_RUNTIME=app_platform`` is set in the
   App Platform environment, the resolver consults
   ``do-connecting-ip`` unconditionally as the primary source. This
   is safe because (a) the header is only writable by DO's ingress
   layer, (b) the env var is set by us (terraform / DO spec), not by
   the request. Docs:
   https://docs.digitalocean.com/support/where-can-i-find-the-client-ip-address-of-a-request-connecting-to-my-app/

Spoof resistance:

- The XFF walk is right-to-left, not left-to-right. nginx appends the
  immediate peer to the right (overwritten to ``$remote_addr`` in our
  config), so the rightmost entries are our own infrastructure.
  Walking from the right and stopping at the first non-trusted entry
  returns the IP just outside our trust boundary - the real client.
  Walking from the LEFT would return whatever the user-controllable
  side put there, which is the textbook XFF-spoof CVE shape.
- The XFF walk only runs when the direct TCP peer is itself a
  trusted proxy. A caller reaching the backend over a public path
  (bypassing our proxy) cannot trigger the walk, so they cannot
  forge any value via XFF.
- ``do-connecting-ip`` is honoured unconditionally only when
  ``PFV_RUNTIME=app_platform`` is set. In any other runtime it is
  ignored entirely (the trusted-peer gate from PR #82 no longer
  applies because the XFF walk replaces it).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Iterable

import structlog
from slowapi import Limiter
from starlette.requests import Request

from app.config import settings
from app.rate_limit_failopen import wrap_limiter_failopen

logger = structlog.stdlib.get_logger()


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


def _is_app_platform_runtime() -> bool:
    """Read at call time (not import time) so tests using
    ``monkeypatch.setenv`` see the change without reloading the module.
    """
    return os.environ.get("PFV_RUNTIME", "").lower() == "app_platform"


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP for rate limiting and audit logging.

    Resolution order:

    1. **DO App Platform mode.** When ``PFV_RUNTIME=app_platform`` is
       set, consult ``do-connecting-ip`` unconditionally. DO's ingress
       is the only writer of that header in App Platform, and the env
       var is set by us (not by the request) so this is not spoofable.
    2. **XFF right-to-left walk** when the direct TCP peer is a
       trusted proxy. The rightmost entry was appended by our own
       nginx (set to ``$remote_addr``); walk leftward, skipping
       trusted-proxy hops, and return the first non-trusted entry.
       That is the IP immediately outside our trust boundary - the
       real client. Returns ``request.client.host`` if every entry
       in the chain is a trusted proxy.
    3. **Direct peer** otherwise. A caller reaching the backend over
       a public path is its own client IP; we refuse to honour any
       forwarded-by headers in that case.
    """
    # 1. DO App Platform runtime: do-connecting-ip is authoritative.
    if _is_app_platform_runtime():
        do_ip = request.headers.get("do-connecting-ip")
        if do_ip:
            return do_ip
        # Header missing - fall through to the standard path so
        # platform health checks etc. still resolve sensibly.

    client = request.client
    client_host = client.host if client else None
    peer_trusted = _is_trusted_proxy(client_host)

    # 2. Trusted peer + XFF chain: walk RIGHT-to-LEFT.
    # nginx's ``X-Forwarded-For $remote_addr`` plus any intermediate
    # proxies produce a chain where the rightmost entries are ours.
    # The first non-trusted entry from the right is the real client.
    if peer_trusted:
        xff_entries = _parse_xff(request.headers.get("x-forwarded-for"))
        for entry in reversed(xff_entries):
            if not _is_trusted_proxy(entry):
                return entry
        # Every entry in the chain was a trusted proxy (or chain was
        # empty). Fall back to ``do-connecting-ip`` for older DO
        # deployments not yet running with PFV_RUNTIME set, then to
        # the direct peer.
        do_ip = request.headers.get("do-connecting-ip")
        if do_ip:
            return do_ip

    # 3. Direct public peer (or no client). Return the peer IP and
    # refuse to honour any forwarded-by headers (they could be forged).
    return client_host or "127.0.0.1"


def _build_limiter() -> Limiter:
    """Construct the slowapi ``Limiter`` with Redis-backed storage when
    ``settings.redis_url`` is configured, else fall back to in-memory.

    Cross-replica accuracy (K8S-1, L0.6 audit). slowapi's default
    in-memory storage keeps counters per-process, so once we scale
    horizontally each replica enforces its own private budget. Pointing
    the limiter at the same Redis the rest of the app already uses
    (see ``redis_client.py``) makes the budget shared across replicas.

    Storage shape: ``storage_uri="redis://..."`` is passed to the
    ``Limiter`` constructor. slowapi delegates to ``limits`` which
    instantiates its own Redis client from the URI. The app's
    ``redis_client.get_client()`` is not directly reused because the
    ``slowapi.Limiter`` constructor (v0.1.9) accepts only a URI string
    plus ``storage_options: Dict[str, str]``, not an already-built
    client/pool.

    Fallback: if ``settings.redis_url`` is empty (local dev without the
    compose Redis service), we keep the in-memory storage and warn so
    the gap is visible in logs. Production / compose always set the URL.
    """
    redis_url = settings.redis_url
    if redis_url:
        logger.info(
            "rate_limit.storage",
            backend="redis",
            multi_replica_safe=True,
        )
        limiter = Limiter(key_func=get_client_ip, storage_uri=redis_url)
        # Fail-open on Redis storage errors (prod hotfix 2026-05-13). The
        # wrapper sits below slowapi so transient Redis blips no longer
        # surface as HTTP 500 from rate-limited auth endpoints. See
        # app/rate_limit_failopen.py for the design + trade-off note.
        wrap_limiter_failopen(limiter)
        return limiter

    logger.warning(
        "rate_limit.storage",
        backend="memory",
        multi_replica_safe=False,
        reason="settings.redis_url empty; per-replica counters only",
    )
    # No fail-open wrap for the in-memory backend: MemoryStorage cannot
    # raise the storage errors the wrapper guards against, and leaving
    # it unwrapped keeps construction-time tests (which assert the
    # storage type) unchanged.
    return Limiter(key_func=get_client_ip)


limiter = _build_limiter()
