"""Redis / Valkey client — singleton, lazy-initialized from settings.

Scope today: MFA email-code single-use nonces. Intentionally narrow. More
features (rate-limit storage, cache, session ACLs) land here when the app
moves off single-replica on DO App Platform and needs shared state.

Behavior when `settings.redis_url` is empty:
- Development: `get_client()` returns `None`. Callers must handle None and
  decide whether to skip the Redis-backed check (usually yes in dev).
- Production: a runtime warning is logged at startup. The security-critical
  callers (MFA nonce) MUST fail closed if they need Redis and it's missing;
  see `require_client()` below.
"""

import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


def get_client() -> Redis | None:
    """Return the Redis client if configured, else None.

    Idempotent. The client is shared across the process lifetime — the
    underlying connection pool handles concurrency.
    """
    global _client
    if _client is None and settings.redis_url:
        _client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _client


async def close_client() -> None:
    """Close the Redis client. Called from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class RedisRequired(RuntimeError):
    """Raised when a caller requires Redis but `settings.redis_url` is empty."""


def require_client() -> Redis:
    """Return the Redis client, raising if it isn't configured.

    Use this from security-critical paths (MFA nonce, token-family revocation)
    where a missing Redis must fail closed, not be silently skipped.
    """
    client = get_client()
    if client is None:
        raise RedisRequired(
            "REDIS_URL must be set for this operation. "
            "Configure it in DO App Platform secrets or your local .env."
        )
    return client
