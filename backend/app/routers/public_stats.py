"""Public, count-only stats for the marketing / apex site.

The apex landing is a static export (no server runtime), so it fetches
this cross-origin from the browser. A bearer token would be exposed in
the public bundle and protect nothing, so this endpoint is intentionally
PUBLIC and returns only a single non-sensitive integer — the
founding-members count the landing page advertises.

Hardened: cached in Redis (5 min) to absorb read volume, rate-limited,
and it never 500s — a Redis or DB hiccup degrades to a best-effort
direct count. Excludes the configured non-real usernames (smoke / seed
accounts) so the public number reflects real founders only.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis_client
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter

logger = structlog.stdlib.get_logger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public"])


@router.get("/founder-count")
@limiter.limit("60/minute")
async def founder_count(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Return ``{"count": <int>}`` — the number of active founding members,
    excluding the configured non-real usernames. Public, cached, never 500s.
    """
    try:
        cached = await redis_client.founder_count_cache_get()
    except Exception:  # noqa: BLE001 — cache is best-effort
        cached = None
    if cached is not None:
        return {"count": cached}

    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.is_founder.is_(True), User.is_active.is_(True))
    )
    excluded = settings.founder_count_exclude_list
    if excluded:
        stmt = stmt.where(User.username.notin_(excluded))
    try:
        count = int(await db.scalar(stmt) or 0)
    except Exception:  # noqa: BLE001 — a public counter must never 500
        # A DB hiccup on a cold cache must not surface a 500 to anonymous
        # callers. Degrade to 0 (the page hides the counter when count<=0).
        logger.warning("public.founder_count.db_failed")
        return {"count": 0}

    try:
        await redis_client.founder_count_cache_set(count)
    except Exception:  # noqa: BLE001 — caching failure must not 500 the read
        pass
    return {"count": count}
