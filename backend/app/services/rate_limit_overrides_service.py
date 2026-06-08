"""CRUD + resolver for per-org / per-user rate-limit overrides (L4.10).

The resolver is the runtime hot path: slowapi invokes it on every
limited request, so the implementation MUST:

1. Be cheap. A short Redis-backed cache (60 s TTL) sits in front of
   the DB lookup. A negative-cache row ("no override exists") uses a
   sentinel string so the resolver does not re-hit MySQL on every
   request from an org with no overrides.
2. Fail open. If Redis is down or the DB lookup fails, the resolver
   returns ``None`` and the decorator's default limit applies. This
   matches the project-wide rate-limit failure stance (see
   ``rate_limit_failopen.py``).
3. Respect ``expires_at``. A row past its expiry is treated as absent
   without being deleted, so the audit history is preserved.

Resolution order (architect-locked):

1. User override for the endpoint key.
2. Org override for the endpoint key.
3. None — caller applies the decorator default.

Cache shape:

- Key: ``rate_limit_override:user:{user_id}:{endpoint_pattern}`` OR
  ``rate_limit_override:org:{org_id}:{endpoint_pattern}``.
- Value: ``"<max>/<period_s>"`` for a hit, ``"-"`` for a miss.
- TTL: 60 s. Invalidated on every mutation that touches the row.

The CRUD entry points each invalidate the affected cache keys so an
admin update takes effect within one TTL window worst-case (faster
when the writer's process clears its own cache, slower across other
replicas — bounded by 60 s either way).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Sequence

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rate_limit_override import RateLimitOverride
from app.services.list_query import resolve_order_by


logger = structlog.stdlib.get_logger()


# Closed whitelist of sortable columns for the admin rate-limit-override
# list. Keys are the public sort tokens the frontend sends; values are
# the column to order by. Anything not here is a 400 (see
# ``list_query.resolve_order_by``).
_SORTABLE = {
    "created_at": RateLimitOverride.created_at,
    "endpoint_pattern": RateLimitOverride.endpoint_pattern,
    "max_requests": RateLimitOverride.max_requests,
    "period_seconds": RateLimitOverride.period_seconds,
    "expires_at": RateLimitOverride.expires_at,
}

# Sentinel stored in Redis when a lookup hits no row. Distinct from
# the wire format of a real override ("<max>/<period>") so the
# resolver can tell a cached miss from a cached hit in one string
# compare.
_NEGATIVE_CACHE_VALUE = "-"
_CACHE_TTL_SECONDS = 60


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_expired(row: RateLimitOverride) -> bool:
    return row.expires_at is not None and row.expires_at <= _utcnow_naive()


def _to_wire(row: RateLimitOverride) -> str:
    """Render an override as ``"<max>/<period_s>"`` for the cache /
    return-from-resolver path. The string is deliberately not in
    slowapi's "N/minute" textual form — the caller composes the
    slowapi-compatible string from the numeric parts so unit mapping
    stays in one place (the limit-string formatter).
    """
    return f"{row.max_requests}/{row.period_seconds}"


def _cache_key(*, scope: str, scope_id: int, endpoint_pattern: str) -> str:
    return f"rate_limit_override:{scope}:{scope_id}:{endpoint_pattern}"


# ---- CRUD ------------------------------------------------------------------


async def create_override(
    db: AsyncSession,
    *,
    org_id: Optional[int],
    user_id: Optional[int],
    endpoint_pattern: str,
    max_requests: int,
    period_seconds: int,
    expires_at: Optional[datetime],
    created_by_user_id: Optional[int],
    note: Optional[str],
) -> RateLimitOverride:
    """Insert a new override row.

    The exactly-one-of-(org_id, user_id) invariant is enforced by the
    Pydantic create schema, so this function asserts it as a belt-and-
    braces guard but never expects the assertion to fire from a
    router call.
    """
    if (org_id is None) == (user_id is None):
        raise ValueError("exactly one of org_id or user_id must be set")
    row = RateLimitOverride(
        org_id=org_id,
        user_id=user_id,
        endpoint_pattern=endpoint_pattern,
        max_requests=max_requests,
        period_seconds=period_seconds,
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
        note=note,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await _invalidate_cache(row)
    return row


async def update_override(
    db: AsyncSession,
    *,
    row: RateLimitOverride,
    patch: dict,
) -> RateLimitOverride:
    """Apply a partial patch (already type-checked by Pydantic) and
    invalidate the cache for both pre- and post-patch endpoint
    patterns. The double-invalidate matters when ``endpoint_pattern``
    moves: the old key would otherwise linger as a stale hit until
    its TTL expires.
    """
    old_pattern = row.endpoint_pattern
    for field, value in patch.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    await _invalidate_cache(row)
    if row.endpoint_pattern != old_pattern:
        await _invalidate_cache_for(
            scope="user" if row.user_id is not None else "org",
            scope_id=row.user_id if row.user_id is not None else row.org_id,
            endpoint_pattern=old_pattern,
        )
    return row


async def delete_override(
    db: AsyncSession,
    *,
    row: RateLimitOverride,
) -> None:
    """Hard-delete and invalidate the cache key."""
    await _invalidate_cache(row)
    await db.delete(row)
    await db.commit()


async def get_by_id(
    db: AsyncSession, override_id: int
) -> Optional[RateLimitOverride]:
    return await db.get(RateLimitOverride, override_id)


async def list_overrides(
    db: AsyncSession,
    *,
    org_id: Optional[int] = None,
    user_id: Optional[int] = None,
    endpoint_pattern: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[Sequence[RateLimitOverride], int]:
    """List overrides with optional scope filters. Returns
    ``(items, total)`` to support a paginated admin table.

    ``sort_by`` is resolved against a closed whitelist (see
    ``_SORTABLE``); an unknown key raises ``ValidationError`` (router →
    400). Defaults to ``created_at`` desc with an ``id`` desc tiebreaker.
    """
    base = select(RateLimitOverride)
    filters_q = base
    if org_id is not None:
        filters_q = filters_q.where(RateLimitOverride.org_id == org_id)
    if user_id is not None:
        filters_q = filters_q.where(RateLimitOverride.user_id == user_id)
    if endpoint_pattern:
        filters_q = filters_q.where(
            RateLimitOverride.endpoint_pattern == endpoint_pattern
        )

    # Count via the SAME filter chain. ``filters_q`` shares its
    # ``whereclause`` with ``filters_q.with_only_columns(...)``, so
    # rebuilding the SELECT with ``func.count()`` reuses every filter
    # the listing path applies without re-stating them by hand.
    count_q = filters_q.with_only_columns(
        func.count(RateLimitOverride.id)
    ).order_by(None)
    total_result = await db.execute(count_q)
    total = int(total_result.scalar() or 0)

    order_by = resolve_order_by(
        sort_by,
        sort_dir,
        allowed=_SORTABLE,
        default_key="created_at",
        default_dir="desc",
        tiebreaker=RateLimitOverride.id.desc(),
    )

    rows_result = await db.execute(
        filters_q.order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    return list(rows_result.scalars().all()), total


# ---- Resolver (hot path) ---------------------------------------------------


async def resolve_override(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    org_id: Optional[int],
    endpoint_pattern: str,
) -> Optional[str]:
    """Return the cached/DB override as ``"<max>/<period_s>"`` or
    ``None`` if no active override exists.

    Resolution order:

    1. User override if any.
    2. Org override if any.
    3. None.

    Both lookups are individually cached so a user with no override
    in an org-with-no-override does not hit MySQL on every request.
    """
    # 1. User scope first.
    if user_id is not None:
        cached = await _cache_get(
            scope="user", scope_id=user_id, endpoint_pattern=endpoint_pattern
        )
        if cached is not None:
            if cached == _NEGATIVE_CACHE_VALUE:
                pass  # fall through to org
            else:
                return cached
        else:
            row = await _fetch_active(
                db,
                scope_col="user_id",
                scope_id=user_id,
                endpoint_pattern=endpoint_pattern,
            )
            if row is not None:
                wire = _to_wire(row)
                await _cache_set(
                    scope="user",
                    scope_id=user_id,
                    endpoint_pattern=endpoint_pattern,
                    value=wire,
                )
                return wire
            await _cache_set(
                scope="user",
                scope_id=user_id,
                endpoint_pattern=endpoint_pattern,
                value=_NEGATIVE_CACHE_VALUE,
            )

    # 2. Org scope.
    if org_id is not None:
        cached = await _cache_get(
            scope="org", scope_id=org_id, endpoint_pattern=endpoint_pattern
        )
        if cached is not None:
            if cached == _NEGATIVE_CACHE_VALUE:
                return None
            return cached
        row = await _fetch_active(
            db,
            scope_col="org_id",
            scope_id=org_id,
            endpoint_pattern=endpoint_pattern,
        )
        if row is not None:
            wire = _to_wire(row)
            await _cache_set(
                scope="org",
                scope_id=org_id,
                endpoint_pattern=endpoint_pattern,
                value=wire,
            )
            return wire
        await _cache_set(
            scope="org",
            scope_id=org_id,
            endpoint_pattern=endpoint_pattern,
            value=_NEGATIVE_CACHE_VALUE,
        )

    return None


async def _fetch_active(
    db: AsyncSession,
    *,
    scope_col: str,
    scope_id: int,
    endpoint_pattern: str,
) -> Optional[RateLimitOverride]:
    """Return the most-recent non-expired override matching the scope
    and endpoint, or ``None``. ``ORDER BY id DESC`` resolves
    multi-row collisions deterministically (newest wins) without
    forcing a unique constraint on the DB row.
    """
    now = _utcnow_naive()
    col = getattr(RateLimitOverride, scope_col)
    q = (
        select(RateLimitOverride)
        .where(col == scope_id)
        .where(RateLimitOverride.endpoint_pattern == endpoint_pattern)
        .where(
            or_(
                RateLimitOverride.expires_at.is_(None),
                RateLimitOverride.expires_at > now,
            )
        )
        .order_by(RateLimitOverride.id.desc())
        .limit(1)
    )
    result = await db.execute(q)
    return result.scalars().first()


# ---- Cache plumbing --------------------------------------------------------
#
# Lazy import of ``redis_client`` keeps the test substrate (which
# doesn't run Redis) from spinning up a client at import time. The
# helpers tolerate Redis being unavailable: every Redis-touching call
# is wrapped in a try/except that logs at debug and returns the
# fail-open value (None / silent miss). Rate limiting fails open as
# the project-wide stance; cache failure must not degrade UX.


async def _cache_get(
    *, scope: str, scope_id: int, endpoint_pattern: str
) -> Optional[str]:
    try:
        from app.redis_client import get_client

        client = get_client()
        if client is None:
            return None
        key = _cache_key(
            scope=scope, scope_id=scope_id, endpoint_pattern=endpoint_pattern
        )
        # redis-py sync client is fine on this path: the cached call
        # is sub-millisecond on a hit, and the surrounding context
        # is already wrapped by ``rate_limit_failopen``. Async-redis
        # migration is a separate work item.
        value = client.get(key)
        if value is None:
            return None
        # redis-py returns bytes by default.
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value
    except Exception as exc:  # noqa: BLE001 — fail open.
        logger.debug("rate_limit_override.cache_get_failed", error=str(exc))
        return None


async def _cache_set(
    *, scope: str, scope_id: int, endpoint_pattern: str, value: str
) -> None:
    try:
        from app.redis_client import get_client

        client = get_client()
        if client is None:
            return
        key = _cache_key(
            scope=scope, scope_id=scope_id, endpoint_pattern=endpoint_pattern
        )
        client.set(key, value, ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail open.
        logger.debug("rate_limit_override.cache_set_failed", error=str(exc))


async def _invalidate_cache(row: RateLimitOverride) -> None:
    scope = "user" if row.user_id is not None else "org"
    scope_id = row.user_id if row.user_id is not None else row.org_id
    if scope_id is None:
        return
    await _invalidate_cache_for(
        scope=scope, scope_id=scope_id, endpoint_pattern=row.endpoint_pattern
    )


async def _invalidate_cache_for(
    *, scope: str, scope_id: int, endpoint_pattern: str
) -> None:
    try:
        from app.redis_client import get_client

        client = get_client()
        if client is None:
            return
        key = _cache_key(
            scope=scope, scope_id=scope_id, endpoint_pattern=endpoint_pattern
        )
        client.delete(key)
    except Exception as exc:  # noqa: BLE001 — fail open.
        logger.debug(
            "rate_limit_override.cache_invalidate_failed",
            error=str(exc),
        )
