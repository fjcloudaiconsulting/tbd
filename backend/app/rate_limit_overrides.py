"""Runtime integration between slowapi and the override table (TBD-492).

Two halves, joined by a ``ContextVar``:

- ``load_rate_limit_overrides`` is an async FastAPI dependency attached to
  every overridable route (``dependencies=[Depends(...)]``). It runs after
  auth, reads the caller's active override rows in ONE query on its own
  short session, and stores ``{endpoint_pattern: "N/period"}`` in
  ``_overrides_cv``.
- ``dynamic_limit(pattern, default)`` is the zero-arg provider slowapi calls
  (``LimitGroup.__iter__``) when the wrapped route runs, i.e. after the
  dependencies. It returns the caller's override for ``pattern`` or
  ``default``.

It must be an ``async`` dependency: FastAPI runs a sync one in a threadpool
copy of the context, so the ``set`` would never reach the route.

Failure stance: any loader error, or an unusable override string, means the
static default applies (project-wide rate-limit fail-open posture).

Pre-auth routes (``PRE_AUTH_ENDPOINT_PATTERNS``) have no identity when the
limiter runs and keep static string limits; tune them in code.

Known limits: the bucket key is still the client IP, and changing an
override starts a fresh counter (the ``limits`` storage key embeds the
amount).
"""
import contextvars
import re
from datetime import datetime, timezone
from typing import Callable, Mapping

import structlog
from fastapi import Depends
from limits import parse_many
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_current_user, get_session_factory
from app.models.rate_limit_override import RateLimitOverride
from app.models.user import User


logger = structlog.stdlib.get_logger()

_overrides_cv: contextvars.ContextVar[Mapping[str, str]] = contextvars.ContextVar(
    "rate_limit_overrides", default={}
)


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
    """Format ``(max, period_s)`` as a string ``limits.parse_many`` accepts.

    Standard buckets use the period word; anything else is
    ``"N/P seconds"`` (a bare ``"N/P"`` is rejected by ``parse_many`` and
    slowapi then drops the limit, leaving the route unlimited).
    """
    if period_seconds == 1:
        return f"{max_requests}/second"
    if period_seconds == 60:
        return f"{max_requests}/minute"
    if period_seconds == 3600:
        return f"{max_requests}/hour"
    if period_seconds == 86400:
        return f"{max_requests}/day"
    return f"{max_requests}/{period_seconds} seconds"


def _usable(limit: str) -> bool:
    """``parse_many`` accepts it and every item allows >=1 request per >=1 period."""
    try:
        items = parse_many(limit)
    except Exception:  # noqa: BLE001
        return False
    return bool(items) and all(i.amount >= 1 and i.multiples >= 1 for i in items)


async def load_rate_limit_overrides(
    user: User = Depends(get_current_user),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> None:
    """Populate ``_overrides_cv`` for this request. Never raises.

    Own short session, so a failed query cannot poison the route's session.
    Org rows first, then user rows overwrite (user beats org); ``ORDER BY id``
    makes the newest row win within a scope.
    """
    overrides: dict[str, str] = {}
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        user_id, org_id = user.id, user.org_id
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(RateLimitOverride)
                    .where(
                        or_(
                            RateLimitOverride.user_id == user_id,
                            RateLimitOverride.org_id == org_id,
                        )
                    )
                    .where(
                        or_(
                            RateLimitOverride.expires_at.is_(None),
                            RateLimitOverride.expires_at > now,
                        )
                    )
                    .order_by(RateLimitOverride.id.asc())
                )
            ).scalars().all()
        by_org: dict[str, str] = {}
        by_user: dict[str, str] = {}
        for r in rows:
            if r.max_requests < 1 or r.period_seconds < 1:
                continue
            target = by_user if r.user_id == user_id else by_org
            target[r.endpoint_pattern] = format_limit(r.max_requests, r.period_seconds)
        overrides = {**by_org, **by_user}
    except Exception as exc:  # noqa: BLE001 — fail open to the defaults.
        logger.warning("rate_limit_override.load_failed", error=str(exc))
        overrides = {}
    _overrides_cv.set(overrides)


def dynamic_limit(endpoint_pattern: str, default: str) -> Callable[[], str]:
    """Zero-arg slowapi limit provider: the caller's override, else ``default``.

    ``default`` is validated at construction so a typo crashes import, not the
    first request. ``.pattern`` / ``.default`` are read by the fences.
    """
    parse_many(default)

    def provider() -> str:
        override = _overrides_cv.get().get(endpoint_pattern)
        return override if override is not None and _usable(override) else default

    provider.pattern = endpoint_pattern  # type: ignore[attr-defined]
    provider.default = default  # type: ignore[attr-defined]
    return provider
