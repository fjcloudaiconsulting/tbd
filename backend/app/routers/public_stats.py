"""Public, count-only stats for the marketing / apex site.

The apex landing is a static export (no server runtime), so it fetches
this cross-origin from the browser. A bearer token would be exposed in
the public bundle and protect nothing, so this endpoint is intentionally
PUBLIC and returns only a single non-sensitive integer — the
founding-members count the landing page advertises.

Hardened: cached in process (5 min) to absorb read volume, rate-limited,
and it never 500s: a DB hiccup degrades to 0. Excludes the configured non-real usernames (smoke / seed
accounts) so the public number reflects real founders only.
"""
from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public"])

# In-process cache (per worker): (monotonic time, count).
_CACHE_TTL_S = 300
_cache: tuple[float, int] | None = None


@router.get("/founder-count")
@limiter.limit("60/minute")
async def founder_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Return ``{"count": <int>}`` — the number of active founding members,
    excluding the configured non-real usernames. Public, cached, never 500s.
    """
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL_S:
        return {"count": _cache[1]}

    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.is_founder.is_(True), User.is_active.is_(True))
    )
    excluded = settings.founder_count_exclude_list
    if excluded:
        stmt = stmt.where(User.username.notin_(excluded))
    elif settings.app_env == "production":
        # TBD-371. An empty list is not an error the query can surface: the
        # count simply comes back one too high (the smoke account is a real,
        # active founder row) and the page renders it. Nothing else in the
        # system notices. This is deliberately a LOG and not a refusal --
        # the blast radius of the value being unset is one wrong integer on
        # a marketing counter, and refusing would trade that for an outage.
        logger.error("public.founder_count.no_exclusions")
    try:
        count = int(await db.scalar(stmt) or 0)
    except Exception:  # noqa: BLE001 — a public counter must never 500
        # A DB hiccup on a cold cache must not surface a 500 to anonymous
        # callers. Degrade to 0 (the page hides the counter when count<=0).
        logger.warning("public.founder_count.db_failed")
        return {"count": 0}

    _cache = (time.monotonic(), count)
    return {"count": count}
