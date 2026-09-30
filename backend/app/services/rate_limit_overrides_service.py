"""CRUD for per-org / per-user rate-limit overrides (L4.10).

The runtime read path lives in ``app.rate_limit_overrides`` (one query per
request, no cache), so a write here takes effect on the next request.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rate_limit_override import RateLimitOverride
from app.services.list_query import resolve_order_by


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
    return row


async def update_override(
    db: AsyncSession,
    *,
    row: RateLimitOverride,
    patch: dict,
) -> RateLimitOverride:
    """Apply a partial patch (already type-checked by Pydantic)."""
    for field, value in patch.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_override(
    db: AsyncSession,
    *,
    row: RateLimitOverride,
) -> None:
    """Hard-delete the row."""
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
