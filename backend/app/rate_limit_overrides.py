"""Runtime integration between slowapi and the override resolver.

This module is the bridge from a ``@limiter.limit(...)`` decorator
site to the per-org / per-user override table:

- ``dynamic_limit("auth.login", "20/minute")`` returns a slowapi-
  compatible *callable* the decorator accepts. On every request the
  callable consults the override resolver and returns either the
  override's limit string (formatted into slowapi's "N/Ns" shape) or
  the original default.
- ``parse_default_limit("20/minute")`` is the inverse formatter used
  by tests + sanity assertions; it accepts the same set of
  ``period`` words slowapi accepts.

Why a callable rather than rewriting every decorator at registration
time: slowapi's decorator captures the limit value at *decorator-
import time* — too early to know the requester's identity or to
consult Redis. The Limiter class explicitly accepts a callable that
receives the request, evaluated per call (see slowapi extension.py
``StrOrCallableStr = Union[str, Callable[..., str]]``).

Failure stance. If the resolver raises, or Redis is unavailable, or
the JWT is unreadable, the helper returns the default. That matches
the project-wide rate-limit fail-open posture and prevents an
override-system bug from locking out the platform.

Pre-auth limitation. ``dynamic_limit()`` requires an authenticated
identity (a ``user_id`` or ``org_id`` extractable from the request)
to resolve org / user overrides. Pre-auth endpoints (login,
register, password-reset request, check-username, email
verification, MFA challenge step, invitation preview / accept, the
cookie-only refresh ``/verify`` route, etc.) have no Bearer JWT yet
when the limiter callable runs, so they always fall back to the
static default regardless of whether an override row exists. This
is by design; per-identity throttling is meaningless before an
identity is known. Pre-auth rate limits should be tuned by editing
the static slowapi decorator default itself, not by creating an
override. The full list of pre-auth patterns lives in
``app.rate_limit_endpoint_catalogue.PRE_AUTH_PATTERNS`` and the
admin UI surfaces a warning when one of those patterns is picked.
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable, Optional

import structlog
from starlette.requests import Request

from app.config import settings


logger = structlog.stdlib.get_logger()


# Slowapi accepts these period words. Mapped to seconds so the
# numeric override format ("max/period_s") can be round-tripped back
# into a slowapi-style string the limiter understands.
_PERIOD_WORDS_TO_SECONDS: dict[str, int] = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
}


_LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)?\s*([a-zA-Z]+)?\s*$")


def parse_default_limit(value: str) -> tuple[int, int]:
    """Parse a slowapi-style limit string into ``(max, period_s)``.

    Accepted shapes:
    - ``"20/minute"``  -> ``(20, 60)``
    - ``"5/hour"``     -> ``(5, 3600)``
    - ``"30/45s"``     -> ``(30, 45)`` (rare; slowapi also accepts ``"30/45 second"``)
    - ``"30/45"``      -> ``(30, 45)``

    Raises ``ValueError`` on an unparseable string. The function is
    deliberately permissive on whitespace and case to match slowapi's
    own parser.
    """
    m = _LIMIT_RE.match(value)
    if not m:
        raise ValueError(f"unparseable limit string: {value!r}")
    max_requests = int(m.group(1))
    explicit_seconds = m.group(2)
    word = (m.group(3) or "").lower()
    if explicit_seconds and word:
        raise ValueError(f"limit cannot mix explicit seconds and word: {value!r}")
    if explicit_seconds is not None:
        period_seconds = int(explicit_seconds)
    elif word:
        seconds = _PERIOD_WORDS_TO_SECONDS.get(word)
        if seconds is None:
            raise ValueError(f"unknown period word: {word!r} in {value!r}")
        period_seconds = seconds
    else:
        raise ValueError(f"limit missing period: {value!r}")
    return max_requests, period_seconds


def format_limit(max_requests: int, period_seconds: int) -> str:
    """Format ``(max, period_s)`` as a slowapi-style string.

    Prefers the natural period word ("minute", "hour", "day",
    "second") when the seconds value is one of the standard buckets;
    falls back to the bare-numeric ``N/X`` shape (which slowapi's
    underlying ``limits`` library accepts as "X seconds") for any
    other value.

    The natural-word form round-trips through ``parse_default_limit``
    cleanly and avoids the ``"42/60second"`` glued form which the
    parser explicitly rejects (mixing explicit seconds and a word
    suffix).
    """
    if period_seconds == 1:
        return f"{max_requests}/second"
    if period_seconds == 60:
        return f"{max_requests}/minute"
    if period_seconds == 3600:
        return f"{max_requests}/hour"
    if period_seconds == 86400:
        return f"{max_requests}/day"
    return f"{max_requests}/{period_seconds}"


def _request_identity(request: Request) -> tuple[Optional[int], Optional[int]]:
    """Best-effort extraction of ``(user_id, org_id)`` from a request.

    Tried sources, in order:

    1. ``request.state.user_id`` / ``request.state.org_id`` if the
       request-context middleware has populated them (auth path).
    2. The Authorization Bearer token's JWT payload (``sub`` and
       ``org_id`` claims).
    3. ``(None, None)`` — the resolver falls through to the default.

    Decoding the JWT here without verifying the signature would be a
    spoof vector, so we always go through the proper decoder. Any
    failure path returns the (None, None) tuple — fail open.
    """
    # 1. Try the request-context middleware first; in the auth path it
    # is already populated.
    state_user = getattr(request.state, "user_id", None)
    state_org = getattr(request.state, "org_id", None)
    if state_user is not None or state_org is not None:
        return state_user, state_org

    # 2. Fall back to decoding the Authorization header. Many of the
    # limited endpoints are PRE-auth (e.g. /auth/login) and the
    # middleware hasn't run get_current_user yet, but the decorator
    # call from slowapi happens before the dependency. So we
    # short-circuit on missing/malformed headers.
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None, None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None, None

    try:
        import jwt as pyjwt

        payload = pyjwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": []},
        )
    except Exception:  # noqa: BLE001 — any decode failure: fail open.
        return None, None
    sub = payload.get("sub")
    org_id = payload.get("org_id")
    try:
        user_id = int(sub) if sub is not None else None
    except (TypeError, ValueError):
        user_id = None
    try:
        org_id_int = int(org_id) if org_id is not None else None
    except (TypeError, ValueError):
        org_id_int = None
    return user_id, org_id_int


async def _resolve_async(
    *,
    user_id: Optional[int],
    org_id: Optional[int],
    endpoint_pattern: str,
    default: str,
) -> str:
    # Lazy imports keep test-substrate cold-start cheap and avoid a
    # circular at module-load time (the service imports nothing from
    # this module, but a future cross-import would trip without the
    # lazy form).
    from app.database import async_session
    from app.services import rate_limit_overrides_service as svc

    if user_id is None and org_id is None:
        return default
    try:
        async with async_session() as session:
            wire = await svc.resolve_override(
                session,
                user_id=user_id,
                org_id=org_id,
                endpoint_pattern=endpoint_pattern,
            )
    except Exception as exc:  # noqa: BLE001 — fail open on any DB error.
        logger.warning(
            "rate_limit_override.resolve_failed",
            error=str(exc),
            endpoint=endpoint_pattern,
        )
        return default
    if wire is None or wire == "-":
        return default
    # wire format is "<max>/<period_seconds>"; transform to slowapi.
    try:
        max_str, period_str = wire.split("/", 1)
        return format_limit(int(max_str), int(period_str))
    except (ValueError, IndexError):
        return default


def dynamic_limit(endpoint_pattern: str, default: str) -> Callable[..., str]:
    """Return a slowapi-compatible callable that yields a per-request
    limit string.

    Usage at the decorator site::

        @limiter.limit(dynamic_limit("auth.login", "20/minute"))
        async def login(...):
            ...

    Behaviour:

    - Parses ``default`` once at module import to surface a
      programmer error early (an unparseable default would otherwise
      blow up only when the limiter first runs).
    - Returns a fresh closure per call (slowapi keeps it cached).
    - The closure is sync-callable, but the underlying DB read is
      async — bridged via ``asyncio.run`` on a fresh loop iff no
      running loop is detected (slowapi 0.1.9's evaluate path is
      currently sync but the bridge tolerates both).

    Pre-auth endpoints WILL NOT honour per-org / per-user overrides.
    The closure short-circuits to ``default`` whenever neither a
    ``user_id`` nor an ``org_id`` can be extracted from the request,
    which is always the case for the following patterns:

    - ``auth.check_username``
    - ``auth.forgot_password``
    - ``auth.login``
    - ``auth.mfa_email_code``
    - ``auth.mfa_email_verify``
    - ``auth.mfa_recovery``
    - ``auth.mfa_verify``
    - ``auth.register``
    - ``auth.resend_verification_public``
    - ``auth.verify`` (cookie-based, no Bearer)
    - ``auth.verify_email``
    - ``org_members.accept_invitation``
    - ``org_members.preview_invitation``

    Tune those routes via the static ``@limiter.limit("N/period")``
    string at the decorator site. See module docstring above for the
    full rationale; the catalogue is the authoritative list at
    ``app.rate_limit_endpoint_catalogue.PRE_AUTH_PATTERNS``.
    """
    # Force-parse the default once. If this raises, the import
    # explodes loudly instead of silently shipping a broken decorator.
    parse_default_limit(default)

    def _limit_for_request(request: Request) -> str:
        try:
            user_id, org_id = _request_identity(request)
        except Exception:  # noqa: BLE001 — any extraction failure: default.
            return default
        if user_id is None and org_id is None:
            return default
        # Bridge async resolver into the slowapi sync evaluation path.
        # Two cases:
        # 1. There is no running event loop in the current thread.
        #    -> Use ``asyncio.run``. This is the path when slowapi's
        #       extension calls us from a sync decorator (rare today
        #       under uvicorn but supported).
        # 2. There IS a running loop (the default uvicorn path).
        #    -> ``asyncio.run`` would refuse; we spin a fresh thread
        #       so the blocking call doesn't park the event loop.
        try:
            asyncio.get_running_loop()
            running_loop = True
        except RuntimeError:
            running_loop = False

        coro_factory = lambda: _resolve_async(  # noqa: E731 — local closure
            user_id=user_id,
            org_id=org_id,
            endpoint_pattern=endpoint_pattern,
            default=default,
        )
        try:
            if not running_loop:
                return asyncio.run(coro_factory())
            # Running loop: run on a worker thread with its own loop.
            import concurrent.futures
            import threading

            result_holder: dict = {}

            def _worker():
                loop = asyncio.new_event_loop()
                try:
                    result_holder["v"] = loop.run_until_complete(coro_factory())
                except Exception as exc:  # noqa: BLE001
                    result_holder["err"] = exc
                finally:
                    loop.close()

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            # Slowapi sync-storage path already runs synchronously on
            # the event loop; this thread is bounded by the resolver's
            # internal timeouts (Redis 1s + DB short-pool). We cap at
            # 2s as a hard ceiling to avoid wedging the request.
            t.join(timeout=2.0)
            if t.is_alive():
                logger.warning(
                    "rate_limit_override.resolve_timeout",
                    endpoint=endpoint_pattern,
                )
                return default
            if "err" in result_holder:
                logger.warning(
                    "rate_limit_override.resolve_thread_error",
                    error=str(result_holder["err"]),
                    endpoint=endpoint_pattern,
                )
                return default
            return result_holder.get("v", default)
        except Exception as exc:  # noqa: BLE001 — last-line fail-open.
            logger.warning(
                "rate_limit_override.bridge_failed",
                error=str(exc),
                endpoint=endpoint_pattern,
            )
            return default

    return _limit_for_request
