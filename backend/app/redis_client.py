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

import asyncio
import functools
import json
import logging
from typing import Any, Awaitable, Callable, TypeVar

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import ResponseError as RedisResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import settings

logger = logging.getLogger(__name__)

_client: Redis | None = None


# 2026-05-19 transport-normalizer. Production hit a class of failures
# where redis-py's ``health_check_interval`` ping fired against a
# pool-idle connection whose underlying TCP socket had been silently
# dropped by some network device (App Platform NAT, VPC router, droplet
# firewall). uvloop raises ``RuntimeError("unable to perform operation
# on <TCPTransport closed=True ...>; the handler is closed")`` in that
# state. ``RuntimeError`` is NOT a ``RedisError`` subclass, so it
# escaped every router-level ``except (RedisRequired, RedisError)``
# handler and FastAPI returned a 500. See ``_normalize_transport_errors``
# below for the narrow translation contract.
_TRANSPORT_DEAD_MARKERS: tuple[str, ...] = (
    # uvloop: "unable to perform operation on <TCPTransport closed=True
    # reading=False ...>; the handler is closed"
    "tcptransport closed",
    "handler is closed",
    # generic asyncio transport already in closed state
    "transport closed",
    "transport is closed",
    "closed transport",
    # socket-level errors that sometimes surface as RuntimeError on uvloop
    # instead of bubbling up as OSError
    "broken pipe",
    "connection reset",
    "connection closed",
    "closed socket",
)


def _looks_like_dead_transport(exc: BaseException) -> bool:
    """True iff the exception's message matches a known closed-transport
    state we want to translate into a transient 503 instead of a 500.

    DELIBERATELY narrow: substring match on a small fixed list. Bare
    ``RuntimeError("programmer bug")`` and ``RedisRequired`` (also a
    ``RuntimeError`` subclass) must still propagate — we want loud
    failures on real code bugs and config gaps, not silent
    re-classification as "Redis hiccup".
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSPORT_DEAD_MARKERS)


_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def _normalize_transport_errors(fn: _F) -> _F:
    """Decorator. Wrap an async Redis helper so closed-transport /
    broken-pipe errors raised from inside redis-py/uvloop surface as
    :class:`redis.exceptions.ConnectionError` (a ``RedisError`` subclass).

    Routers already catch ``(RedisRequired, RedisError)`` and return 503
    on those, which the frontend treats as transient and retries on a
    fresh connection. Without this wrapper, the bare ``RuntimeError`` from
    uvloop bypasses the handler and FastAPI returns 500 — see the
    production trace at ``2026-05-19T07:10:52`` for the canonical
    instance this decorator exists to prevent.

    EXPLICITLY preserved unchanged (no re-classification):
      * ``RedisError`` and subclasses — including ``ResponseError`` from
        Lua EVAL. ``session_rotate_lua`` parses ``ResponseError`` messages
        for the Lua-return-token contract (``session_revoked`` /
        ``already_rotated`` / ``jti_collision``); papering over those
        with a generic ``ConnectionError`` would break rotation logic.
      * ``RedisRequired`` — programmer / config signal that ``REDIS_URL``
        is not set. Pass through so production fails loud until fixed.
      * Bare ``RuntimeError`` whose message doesn't match the narrow
        transport-marker list. Almost certainly a real bug; re-raise so
        it surfaces as a 500 with a complete traceback in logs.

    Translated:
      * ``OSError`` subclasses (``ConnectionResetError``,
        ``BrokenPipeError``, ``ConnectionAbortedError``, etc.) — socket
        I/O failures during a Redis op.
      * ``RuntimeError`` whose message matches a transport-death marker.
    """
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except RedisError:
            # ResponseError, ConnectionError, TimeoutError, etc. — already
            # a sensible Redis-domain exception. Pass through so the
            # caller's ``except (RedisRequired, RedisError)`` runs AND so
            # ``session_rotate_lua``'s Lua-return-token parser still sees
            # the raw ResponseError it expects.
            raise
        except RedisRequired:
            # Programmer / config signal. Not a transport problem; pass
            # through so the caller's existing handler logic runs.
            raise
        except OSError as exc:
            # Socket-level I/O. Translate to RedisConnectionError so the
            # router's existing 503 fallback kicks in — AND retire the
            # poisoned pool so the next call uses a fresh connection.
            await _retire_poisoned_client(
                reason=f"OSError: {exc.__class__.__name__}: {exc}",
            )
            raise RedisConnectionError(
                f"Redis transport I/O failure: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc
        except RuntimeError as exc:
            if _looks_like_dead_transport(exc):
                # Same as above: poisoned-pool retirement BEFORE the
                # router's caller hits the next helper. Without this,
                # the frontend's reactive retry would hit the same
                # dead pool again. ``RuntimeError`` is deliberately
                # NOT in ``retry_on_error`` (would also retry real
                # bugs), so redis-py's own disconnect path doesn't
                # run for this class — we must drop the singleton
                # ourselves.
                await _retire_poisoned_client(
                    reason=f"closed transport: {exc}",
                )
                raise RedisConnectionError(
                    f"Redis transport closed: {exc}"
                ) from exc
            # Unrelated RuntimeError — almost certainly a real bug.
            # Let it escape so it surfaces as 500 with full traceback.
            raise

    return wrapper  # type: ignore[return-value]


async def _retire_poisoned_client(*, reason: str) -> None:
    """Drop the module-level Redis singleton so the next ``get_client()``
    creates a fresh client (and fresh underlying connection pool).

    Called by ``_normalize_transport_errors`` after it detects a known
    dead-socket / closed-transport state and BEFORE it raises the
    translated ``RedisConnectionError``. Without this, the frontend's
    reactive 503 retry would loop back to the same poisoned pool and
    the user would see another 503 instead of recovering.

    ``RuntimeError`` is deliberately excluded from
    ``retry_on_error`` (see ``get_client`` rationale), so redis-py's
    own disconnect-on-retry path does NOT run for the uvloop closed-
    transport class; the application has to drop the client itself.

    Best-effort: any failure inside ``aclose()`` is swallowed so we
    don't replace one ConnectionError with another. The next call to
    ``get_client()`` will rebuild the singleton from
    ``settings.redis_url`` regardless.
    """
    global _client
    poisoned = _client
    if poisoned is None:
        return
    _client = None
    logger.warning(
        "redis.client.retired",
        extra={"reason": reason},
    )
    try:
        await asyncio.wait_for(
            poisoned.aclose(), timeout=AUTH_REDIS_ACLOSE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        # Cleanup wedged past the bound. Singleton is already dropped
        # above; the OS will reclaim the FDs. Surface a distinct event
        # so operators can tell a wedged retirement apart from a clean
        # one in production logs.
        logger.warning(
            "redis.client.retired.aclose_timeout",
            extra={"timeout_s": AUTH_REDIS_ACLOSE_TIMEOUT_S},
        )
    except Exception:  # noqa: BLE001 — best-effort cleanup
        # We've already replaced the singleton; the OS will reclaim the
        # underlying sockets even if aclose() can't tidy up its
        # bookkeeping. Don't propagate.
        pass


# Per-operation Redis timeout budget. ``/auth/refresh`` makes up to 7
# sequential Redis calls in the worst case (see math at bottom of
# get_client); the frontend's reactive-recovery cap is 45 s. We need
# the total Redis budget for a single /refresh to come in under that
# so a transient VPC blip surfaces as a fail-fast 503 the frontend
# retries, not a 45 s "(canceled)" (the 2026-05-19T15 production
# trace).
AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S = 1.0
AUTH_REDIS_SOCKET_TIMEOUT_S = 1.0
AUTH_REDIS_RETRY_BACKOFF_BASE_S = 0.05
AUTH_REDIS_RETRY_BACKOFF_CAP_S = 0.2
AUTH_REDIS_RETRY_COUNT = 1
# Cap for ``_retire_poisoned_client``'s best-effort ``aclose()``.
# Pool cleanup runs outside the per-command socket-timeout budget,
# so a wedged close on a stale connection could hold the translated
# 503 past the frontend's reactive-recovery cap. Bound it at the
# application layer so retirement always completes promptly.
AUTH_REDIS_ACLOSE_TIMEOUT_S = 2.0


def _build_auth_redis_client(redis_url: str) -> Redis:
    """Construct the auth Redis client from ``redis_url`` using the
    fail-fast budget constants. Production entry point is
    ``get_client()``; this helper exists so tests can exercise the
    real builder without fighting the conftest autouse fixture that
    replaces ``get_client`` with a fake-Redis lambda.

    See ``get_client()`` for the budget rationale.
    """
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=AUTH_REDIS_SOCKET_CONNECT_TIMEOUT_S,
        socket_timeout=AUTH_REDIS_SOCKET_TIMEOUT_S,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_error=[
            RedisConnectionError,
            RedisTimeoutError,
            OSError,
        ],
        retry=Retry(
            ExponentialBackoff(
                cap=AUTH_REDIS_RETRY_BACKOFF_CAP_S,
                base=AUTH_REDIS_RETRY_BACKOFF_BASE_S,
            ),
            retries=AUTH_REDIS_RETRY_COUNT,
        ),
    )


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
      very first stale-pool socket. ``OSError`` (covers
      ``ConnectionResetError`` and ``BrokenPipeError``) is in the retry
      list because production observed those classes on idle-dropped
      sockets — the 2026-05-19T07:10 trace. We deliberately do NOT add
      bare ``RuntimeError`` here because redis-py's retry contract is
      "drop connection + reconnect on listed error"; widening to all
      ``RuntimeError`` would also retry genuine programmer bugs. The
      transport-runtime case is handled instead by the
      ``_normalize_transport_errors`` wrapper at the helper layer.

    **Fail-fast budget (2026-05-19 production trace).** Honest worst case
    accounts for both connect and read on the retry attempt because
    redis-py's ``retry_on_error`` contract drops the connection and
    reconnects on the listed exception classes:

        per_call_worst = socket_timeout
                       + retries * (socket_connect_timeout
                                    + socket_timeout
                                    + backoff_cap)
                       = 1.0 + 1 * (1.0 + 1.0 + 0.2)
                       = 3.2 s

    ``/auth/refresh`` makes up to 7 sequential Redis calls in the
    already_rotated branch after PR #315:

      1. ``session_validate(jti)``                            (validator)
      2. ``session_family_member(sid, jti)``     (validator primary path)
      3. ``session_rotate_lua(...)``                          (rotation)
      4. ``session_grace(old_jti)``                           (re-probe)
      5. ``session_family_exists(sid)``                       (re-probe)
      6. ``session_validate(successor_jti)``        (catch-up helper)
      7. ``session_family_member(sid, successor_jti)`` (catch-up helper)

    Worst case for the already_rotated branch: ``7 * 3.2 = 22.4 s``.
    Direct grace branch is 5 calls: ``5 * 3.2 = 16.0 s``. Normal "ok"
    rotation is 3 calls: ``3 * 3.2 = 9.6 s``. All under the frontend's
    45 s reactive-recovery timer, with at least 20 s margin even in
    the pathological case.

    Earlier values (socket_timeout=3, retries=2, cap=1.0) summed to
    per_call ~= ``3 + 2 * (3 + 3 + 1)`` = ``17 s`` honest worst case,
    7 calls = ~119 s — the requests reached the backend, sat
    retrying past the 45 s frontend cancel, and surfaced as the hung
    "(canceled)" entries in the 2026-05-19T15:25–15:42 trace.

    Tests in ``test_auth_redis_failfast_budget.py`` pin the new
    ceiling so any future change that re-inflates the budget is caught
    in CI.
    """
    global _client
    if _client is None and settings.redis_url:
        _client = _build_auth_redis_client(settings.redis_url)
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
#                              TTL = session_lifetime_days * 86400,
#                              resolved per-org at issue/rotation time)
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


@_normalize_transport_errors
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


@_normalize_transport_errors
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


@_normalize_transport_errors
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


@_normalize_transport_errors
async def session_family_exists(sid: str) -> bool:
    """Return True iff ``auth:session:by_sid:{sid}`` exists.

    Defence-in-depth check for the grace-acceptance branch (§5.1 step 4
    and §5.2): a concurrent logout deletes the family set first, so the
    grace key can outlive the family by up to 30 seconds. The resolver
    rejects in that window.
    """
    client = require_client()
    return bool(await client.exists(_family_key(sid)))


@_normalize_transport_errors
async def session_family_member(sid: str, jti: str) -> bool:
    """Return True iff ``jti`` is still a member of ``auth:session:by_sid:{sid}``.

    Architect P1 finding on PR #308: the per-session logout makes
    ``DEL auth:session:by_sid:{sid}`` the load-bearing revocation
    step (Round A of the logout family revoke). Primary keys are
    cleaned up afterwards in Round B. Between Round A landing and
    Round B finishing — or if Round B partially fails — a request
    could find a still-alive ``auth:session:{jti}`` even though the
    session is logically revoked.

    The Lua rotation script catches this on ``/refresh`` via its own
    ``SISMEMBER`` guard (spec §4.2 step 5 check 1). But ``/verify``
    does NOT run the Lua, and the primary-key probe in
    ``_validate_single_refresh_token`` historically only checked
    existence + ``{user_id, sid}`` binding. Membership in the
    family set is the actual authoritative check; this helper
    mirrors the Lua contract for callers that do not run Lua.

    Stronger than ``session_family_exists`` because it also catches
    the impossible-but-NX-defended ``jti`` collision case where two
    sessions happen to share a ``sid`` but only one's ``jti`` is in
    the family set.
    """
    client = require_client()
    return bool(await client.sismember(_family_key(sid), jti))


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
-- ARGV[2] = session TTL seconds (per-org session_lifetime_days * 86400)
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


@_normalize_transport_errors
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


@_normalize_transport_errors
async def session_revoke_family(sid: str) -> list[str]:
    """Atomically revoke an entire session family (spec §5.3 / §4.2 logout).

    Round A: in one ``MULTI/EXEC`` read every ``jti`` in
    ``auth:session:by_sid:{sid}`` THEN delete the family set. The atomic
    delete of the family set is what closes the architect's PR #301
    follow-up race — any concurrent ``/refresh`` Lua rotation will see
    ``SISMEMBER`` return 0 after this lands and refuse to write a
    successor (Section 4.2 step 5 guard 1).

    Round B: for every ``jti`` returned by Round A, delete the primary
    key ``auth:session:{jti}`` AND the grace key
    ``auth:session:grace:{jti}`` in one ``MULTI/EXEC``. Strictly cleanup
    of orphan keys at this point — the family-set delete in Round A is
    the load-bearing step.

    Returns the list of ``jti`` values that were in the family (the
    caller uses the length for the ``auth.session.terminated`` audit
    detail ``jti_count``).

    Fails CLOSED on unreachable Redis via :func:`require_client`.
    """
    client = require_client()
    # Round A — read membership + delete the family set atomically.
    pipe_a = client.pipeline(transaction=True)
    pipe_a.smembers(_family_key(sid))
    pipe_a.delete(_family_key(sid))
    results_a = await pipe_a.execute()
    members = results_a[0] if results_a else set()
    # ``smembers`` may yield ``set`` or ``list`` depending on the client /
    # fake — normalise to a sorted ``list[str]`` for stable iteration and
    # audit-detail reproducibility in tests.
    jtis: list[str] = sorted(str(j) for j in members)

    if not jtis:
        return []

    # Round B — delete every primary + grace key for the revoked family.
    # Strict cleanup; no conditional logic required.
    pipe_b = client.pipeline(transaction=True)
    for jti in jtis:
        pipe_b.delete(_primary_key(jti))
        pipe_b.delete(_grace_key(jti))
    await pipe_b.execute()
    return jtis


# ── MFA single-use nonces ───────────────────────────────────────────────
#
# The /mfa/email-verify path proves an emailed 6-digit code was not
# replayed. The jti embedded in the email JWT is recorded in Redis at
# issue time AND deleted on first successful verify. 0 == not found ==
# replay attempt → 401.
#
# These helpers exist (vs. inline ``get_client().set(...)`` in auth.py)
# so the ``_normalize_transport_errors`` wrapper covers the MFA path
# too — without this layer, a closed-transport RuntimeError during
# /mfa/email-verify produces a 500 instead of a recoverable 503.
#
# Both helpers return ``None`` when Redis isn't configured (dev mode);
# production callers MUST check for that and fail closed with 503.
# 2026-05-19: added by the Redis transport-normalizer PR.


_MFA_EMAIL_JTI_KEY = "mfa_email_jti:{jti}"


@_normalize_transport_errors
async def mfa_email_nonce_set(jti: str, user_id: int, ttl_seconds: int) -> bool:
    """Store the MFA single-use nonce. Returns True if Redis is
    configured and the write landed, False if Redis is not configured
    (dev mode with empty ``REDIS_URL``). Production callers should
    refuse to issue the email token when this returns False.
    """
    client = get_client()
    if client is None:
        return False
    await client.set(
        _MFA_EMAIL_JTI_KEY.format(jti=jti),
        str(user_id),
        ex=ttl_seconds,
    )
    return True


@_normalize_transport_errors
async def mfa_email_nonce_consume(jti: str) -> int | None:
    """Atomically consume the MFA single-use nonce. Returns:
      * ``None`` — Redis not configured (dev mode); caller is
        responsible for failing closed if production.
      * ``0`` — nonce was not in Redis (replay attempt or expired); the
        caller should 401.
      * ``1`` — nonce existed and was deleted in this call; verify
        proceeds.
    """
    client = get_client()
    if client is None:
        return None
    return await client.delete(_MFA_EMAIL_JTI_KEY.format(jti=jti))
