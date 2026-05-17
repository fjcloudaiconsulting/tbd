"""Redis / Valkey client — singleton, lazy-initialized from settings.

Scope today: MFA email-code single-use nonces + refresh-session primary
key, grace key, and family set (``auth:session:{jti}``,
``auth:session:grace:{jti}``, ``auth:session:by_sid:{sid}``).
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
from redis.exceptions import ResponseError as RedisResponseError
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
# Three key shapes drive the per-session story:
#
#   auth:session:{jti}         primary key for ONE refresh JWT (rotates each
#                              /refresh; value = JSON {"user_id", "sid"};
#                              TTL = refresh_idle_ttl_days * 86400)
#   auth:session:grace:{jti}   30s rotation grace key, written by the Lua
#                              script BEFORE the old primary is deleted.
#                              Value JSON {"user_id", "sid", "successor_jti"}.
#                              Read by /refresh + /verify to absorb cross-tab
#                              races without forcing a logout.
#   auth:session:by_sid:{sid}  family set holding every jti ever issued for
#                              this session FAMILY (sid is stable across the
#                              entire rotation chain so per-session logout
#                              in PR 4 can revoke every successor)
#
# Every issue path (login, /refresh rotation, MFA branches, Google
# callback, invitation accept) MUST write both keys in a single
# MULTI/EXEC BEFORE setting the cookie. If Redis is unreachable the
# request returns 503 — never set a cookie that has no Redis row.

SESSION_PRIMARY_KEY = "auth:session:{jti}"
SESSION_GRACE_KEY = "auth:session:grace:{jti}"
SESSION_FAMILY_KEY = "auth:session:by_sid:{sid}"

# Rotation grace window — see spec §2.5. 30s comfortably above worst-case
# cross-tab clock skew, well below access-token TTL so a stolen
# pre-rotation cookie cannot silently extend a session.
SESSION_GRACE_TTL_SECONDS = 30


def _primary_key(jti: str) -> str:
    return f"auth:session:{jti}"


def _grace_key(jti: str) -> str:
    return f"auth:session:grace:{jti}"


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


async def session_grace(jti: str) -> dict | None:
    """Return the JSON payload at ``auth:session:grace:{jti}`` or None on miss.

    Called by ``/refresh`` (app-side step 4) and ``/verify`` (§5.2) when the
    primary key has been rotated out. Caller must additionally verify that
    the JWT's ``sid`` matches the grace row's ``sid`` AND that
    ``auth:session:by_sid:{sid}`` still exists (logout hasn't deleted the
    family).
    """
    client = require_client()
    raw = await client.get(_grace_key(jti))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        # Corrupt key; treat as miss. Belt-and-braces — the rotate Lua
        # script is the only writer of this key and always writes valid
        # JSON.
        return None


async def session_family_exists(sid: str) -> bool:
    """Return True iff ``auth:session:by_sid:{sid}`` exists.

    Defence-in-depth check for the grace-acceptance branch (§5.1 step 4
    and §5.2): a concurrent logout deletes the family set first, so the
    grace key can outlive the family by up to 30 seconds. The resolver
    rejects in that window.
    """
    client = require_client()
    return bool(await client.exists(_family_key(sid)))


# Lua return tokens — spec §4.2 step 5. The router branch table in
# §5.1 step 6 maps these to HTTP behaviour. The bare string ``"ok"``
# is returned on the happy path; the three error tokens come back via
# ``redis.exceptions.ResponseError`` because py-redis surfaces Lua
# ``{err = "..."}`` returns as that exception with the err string as
# the message.
SESSION_ROTATE_OK = "ok"
SESSION_ROTATE_REVOKED = "session_revoked"
SESSION_ROTATE_ALREADY_ROTATED = "already_rotated"
SESSION_ROTATE_JTI_COLLISION = "jti_collision"


# Spec §4.2 step 5. Three guards in order — every guard returns early on
# failure with NO writes. Lua executes atomically server-side so partial
# application is not possible.
ROTATE_SESSION_LUA = """
-- KEYS[1] = auth:session:{old_jti}
-- KEYS[2] = auth:session:grace:{old_jti}
-- KEYS[3] = auth:session:{new_jti}
-- KEYS[4] = auth:session:by_sid:{sid}
-- ARGV[1] = grace TTL seconds (30)
-- ARGV[2] = idle TTL seconds (refresh_idle_ttl_days * 86400)
-- ARGV[3] = grace JSON value
-- ARGV[4] = primary JSON value
-- ARGV[5] = old_jti (string to check SISMEMBER + identify in family)
-- ARGV[6] = new_jti (string to SADD into family)

-- (1) Family revoked? Concurrent /logout deleted the family set.
if redis.call("SISMEMBER", KEYS[4], ARGV[5]) == 0 then
    return {err = "session_revoked"}
end

-- (2) Already rotated? Concurrent /refresh with the same old_jti won the race.
--     The earlier app-side GET cannot prevent two requests reaching this
--     point. This check INSIDE Lua is the authority.
if redis.call("EXISTS", KEYS[1]) == 0 then
    return {err = "already_rotated"}
end

-- (3) Defensive NX on new primary. 128-bit jti collisions are
--     astronomically unlikely but overwriting a live session is the
--     wrong failure mode. SET ... NX returns the bulk string "OK" on
--     success and false (nil bulk) on NX-miss; both ``not result``
--     and ``== false`` match nil in Lua but explicit ``not`` keeps
--     the intent unambiguous.
local set_ok = redis.call("SET", KEYS[3], ARGV[4], "EX", ARGV[2], "NX")
if not set_ok then
    return {err = "jti_collision"}
end

-- (4) Write grace, register the new jti in the family, delete the old primary.
redis.call("SET", KEYS[2], ARGV[3], "EX", ARGV[1])
redis.call("SADD", KEYS[4], ARGV[6])
redis.call("EXPIRE", KEYS[4], ARGV[2])
redis.call("DEL", KEYS[1])
return "ok"
"""


async def session_rotate_lua(
    old_jti: str,
    new_jti: str,
    sid: str,
    user_id: int,
    idle_ttl_seconds: int,
    grace_ttl_seconds: int = SESSION_GRACE_TTL_SECONDS,
) -> str:
    """Run the atomic rotation Lua script (spec §4.2 step 5).

    Returns the bare string ``"ok"`` on success, or one of the three
    error tokens (``session_revoked``, ``already_rotated``,
    ``jti_collision``) when the corresponding guard trips.

    Lua errors come back as :class:`redis.exceptions.ResponseError` in
    py-redis; the message body carries the ``err`` field verbatim. We
    map back to a string return so the router can dispatch on the four
    tokens uniformly.

    Fails CLOSED on unreachable Redis via :func:`require_client`. The
    ``RedisError`` family (connection / timeout / EVAL transport
    failures) bubbles up unchanged so the router can return 503.
    """
    client = require_client()
    primary_value = json.dumps(
        {"user_id": user_id, "sid": sid}, separators=(",", ":")
    )
    grace_value = json.dumps(
        {"user_id": user_id, "sid": sid, "successor_jti": new_jti},
        separators=(",", ":"),
    )
    try:
        result = await client.eval(
            ROTATE_SESSION_LUA,
            4,  # keycount
            _primary_key(old_jti),
            _grace_key(old_jti),
            _primary_key(new_jti),
            _family_key(sid),
            grace_ttl_seconds,
            idle_ttl_seconds,
            grace_value,
            primary_value,
            old_jti,
            new_jti,
        )
    except RedisResponseError as exc:
        # py-redis surfaces Lua ``{err = "..."}`` returns as ResponseError.
        msg = str(exc)
        for token in (
            SESSION_ROTATE_REVOKED,
            SESSION_ROTATE_ALREADY_ROTATED,
            SESSION_ROTATE_JTI_COLLISION,
        ):
            if token in msg:
                return token
        # Genuine Lua error (script bug, transport corruption). Let it
        # propagate so the router can return 503.
        raise
    # Successful return is the literal string "ok" — bytes when
    # decode_responses=False, str when True. The client is configured
    # with decode_responses=True so we expect a str, but accept both
    # for the test fake.
    if isinstance(result, bytes):
        result = result.decode("utf-8")
    return result
