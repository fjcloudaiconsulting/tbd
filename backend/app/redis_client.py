"""Redis / Valkey client — singleton, lazy-initialized from settings.

Scope today: MFA email-code single-use nonces + refresh-session primary
key and family set (``auth:session:{jti}``, ``auth:session:by_sid:{sid}``).
More features (rate-limit storage, cache) land here when the app moves
off single-replica on DO App Platform and needs shared state.

Behavior when `settings.redis_url` is empty:
- Development: `get_client()` returns `None`. Callers must handle None and
  decide whether to skip the Redis-backed check (usually yes in dev).
- Production: a runtime warning is logged at startup. The security-critical
  callers (MFA nonce, auth-session writes) MUST fail closed if they need
  Redis and it's missing; see `require_client()` below.
"""

import json
import logging

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


def get_client() -> Redis | None:
    """Return the Redis client if configured, else None.

    Idempotent. The client is shared across the process lifetime, the
    underlying connection pool handles concurrency.

    Resilience knobs explained:
    - `socket_keepalive=True`: lets the kernel detect dead peers (Valkey
      restart, droplet reboot, VPC route flap) instead of waiting for the
      next read to time out three seconds in.
    - `health_check_interval=30`: redis-py pings idle pooled connections
      every 30s and discards any that fail, so a Valkey restart does not
      leave half-open sockets sitting in the pool waiting to time out a
      future caller.
    - `retry_on_error` + `retry`: one short backoff retry on connect or
      timeout errors. This makes the dashboard probe and the MFA nonce
      path tolerate a Valkey blip (e.g. an Ansible re-converge that
      bounces the service) instead of surfacing a hard failure on the
      very first stale-pool socket.
    """
    global _client
    if _client is None and settings.redis_url:
        _client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
            retry=Retry(ExponentialBackoff(cap=1.0, base=0.1), retries=2),
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


# ── Refresh-session keys (specs/2026-05-17-backend-session-model.md §4) ─────
#
# Two key shapes drive the per-session story:
#
#   auth:session:{jti}        primary key for ONE refresh JWT (rotates each
#                             /refresh; value = JSON {"user_id", "sid"};
#                             TTL = refresh_idle_ttl_days * 86400)
#   auth:session:by_sid:{sid} family set holding every jti ever issued for
#                             this session FAMILY (sid is stable across the
#                             entire rotation chain so per-session logout
#                             in PR 4 can revoke every successor)
#
# Every issue path (login, /refresh rotation, MFA branches, Google
# callback, invitation accept) MUST write both keys in a single
# MULTI/EXEC BEFORE setting the cookie. If Redis is unreachable the
# request returns 503 — never set a cookie that has no Redis row.

SESSION_PRIMARY_KEY = "auth:session:{jti}"
SESSION_FAMILY_KEY = "auth:session:by_sid:{sid}"


def _primary_key(jti: str) -> str:
    return f"auth:session:{jti}"


def _family_key(sid: str) -> str:
    return f"auth:session:by_sid:{sid}"


async def session_issue(jti: str, sid: str, user_id: int, ttl_seconds: int) -> None:
    """Write the refresh-session primary key + family-set entry atomically.

    Runs one ``MULTI/EXEC`` so a partial write cannot leak — either the
    primary key AND the family set entry both land, or neither does.
    Used by every fresh-session issue path (login password, MFA
    branches, Google callback, invitation accept).

    Fails CLOSED on unreachable Redis via :func:`require_client` —
    routers that catch ``RedisRequired`` and other ``RedisError``
    subclasses return 503 and DO NOT set the cookie. See
    ``specs/2026-05-17-backend-session-model.md`` §7.1.
    """
    client = require_client()
    payload = json.dumps({"user_id": user_id, "sid": sid}, separators=(",", ":"))
    pipe = client.pipeline(transaction=True)
    pipe.set(_primary_key(jti), payload, ex=ttl_seconds)
    pipe.sadd(_family_key(sid), jti)
    pipe.expire(_family_key(sid), ttl_seconds)
    await pipe.execute()


async def session_validate(jti: str) -> dict | None:
    """Return the JSON payload at ``auth:session:{jti}`` or None on miss.

    Caller is expected to translate ``None`` into the existing 401
    ``"Session has been invalidated"`` response. Redis connection errors
    bubble up unchanged so the router can return 503 (fail closed).
    """
    client = require_client()
    raw = await client.get(_primary_key(jti))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Corrupt key; treat as miss. Belt-and-braces — this should not
        # happen because session_issue is the only writer and it always
        # writes valid JSON.
        return None


async def session_rotate(
    old_jti: str,
    new_jti: str,
    sid: str,
    user_id: int,
    ttl_seconds: int,
) -> None:
    """Sequential rotation: SET new primary, SADD by_sid new_jti,
    EXPIRE by_sid, DEL old primary — in one ``MULTI/EXEC`` block.

    PR 2 scope: NO Lua, NO EXISTS guards, NO grace key. The architect-
    acknowledged partial-failure window is tolerated for one PR cycle
    because in PR 2 there is no concurrent-logout-vs-rotation surface
    yet (the global-cutoff logout still wipes everything) and no grace
    key for the race in §4.3 to subvert. PR 3 replaces this with the
    full Lua script.

    Fails CLOSED on unreachable Redis via :func:`require_client`.
    """
    client = require_client()
    payload = json.dumps({"user_id": user_id, "sid": sid}, separators=(",", ":"))
    pipe = client.pipeline(transaction=True)
    pipe.set(_primary_key(new_jti), payload, ex=ttl_seconds)
    pipe.sadd(_family_key(sid), new_jti)
    pipe.expire(_family_key(sid), ttl_seconds)
    pipe.delete(_primary_key(old_jti))
    await pipe.execute()
