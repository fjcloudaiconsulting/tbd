"""Description-suggestion autocomplete service (L3.2 Wave 2A).

Implements the contract frozen at
``~/.claude/projects/-Users-fjorge-src-pfv/specs/2026-05-12-l3-2-import-contracts.md``
§5.

Ranking (server-authoritative):
  1. Prefix match first (``description LIKE :q || '%'``) ranked above
     substring matches.
  2. Then frequency (``COUNT(*) DESC``) among same-rank rows.
  3. Then recency (``MAX(date) DESC``) as tiebreaker.

Source: the org's own ``transactions`` table, filtered by ``org_id`` and
``type``. No cross-org leak. No ``merchant_dictionary`` data.

Privacy: the caller (router) MUST NOT log raw descriptions or the raw
``q`` query string. This service does not log either; callers should
emit only ``org_id``, ``type``, ``query_length``, ``result_count`` per
§5.4.

Each returned suggestion carries the description's most-frequently-paired
category for that org. When the same description has been used with
multiple categories, the most-used wins (ties broken by recency).
"""
from __future__ import annotations

import datetime
from typing import Literal

from sqlalchemy import case, desc, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.transaction import Transaction, TransactionType
from app.schemas.transaction_suggestions import DescriptionSuggestion


def _normalize_prefix(q: str) -> str:
    """Escape LIKE metacharacters so a raw user query can't widen the match.

    LIKE in SQL treats ``%`` and ``_`` as wildcards. A user typing
    ``50%`` should not accidentally search for "50 followed by
    anything". Escape with a backslash and pass it to the query.
    """
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_description_suggestions(
    db: AsyncSession,
    *,
    org_id: int,
    type: Literal["income", "expense", "transfer"],
    q: str | None,
    limit: int,
) -> list[DescriptionSuggestion]:
    """Return ranked description suggestions for an org.

    Args:
        db: Async session.
        org_id: Caller's org. ALL queries filter on this — no cross-org
            leak.
        type: Transaction type to filter (matches the manual-entry
            form's selected mode).
        q: Optional search query. Caller is responsible for the 2-char
            minimum (the router rejects shorter ``q`` with 422); this
            service is permissive so service-level tests can assert
            the empty-q (recent items) path.
        limit: Cap on results (1..25 per the schema).

    Returns:
        Up to ``limit`` ``DescriptionSuggestion`` rows, ordered by
        prefix-match → frequency → recency.

    Notes on rank stability:
        - Prefix matches always rank above substring matches.
        - When ``q`` is None or empty, every grouped row carries
          ``prefix_rank = 0`` (no prefix tiering needed) and the
          frequency/recency ordering produces the "top-N most-used"
          recent-items list per §5.3.
    """
    # Map the literal string ``type`` to the SQLAlchemy enum so MySQL's
    # ENUM column matches. SQLAlchemy enum binding accepts the Python
    # enum object directly.
    type_enum = TransactionType(type)

    # ── Phase 1: rank descriptions with aggregates ───────────────────
    # Group by description, count uses, max date, and (when q is set)
    # tier prefix matches above substring matches.
    if q:
        like_arg = _normalize_prefix(q)
        prefix_pat = like_arg + "%"
        substr_pat = "%" + like_arg + "%"
        # SQLite's ``LIKE`` is case-insensitive by default for ASCII;
        # MySQL's default collation (utf8mb4_0900_ai_ci) is likewise
        # case-insensitive. Using ``LIKE`` (with explicit ESCAPE) keeps
        # the query portable. Postgres would need ``ILIKE`` but the
        # codebase targets MySQL.
        prefix_rank_expr = func.sum(
            case(
                (
                    Transaction.description.like(prefix_pat, escape="\\"),
                    1,
                ),
                else_=0,
            )
        )
        # We must restrict to rows that match either prefix OR
        # substring, then derive the rank from whether ANY row in the
        # group is a prefix hit. ``func.sum`` on the CASE delivers
        # >0 if any row of the group matches the prefix; we cast that
        # to a 0/1 tier via ORDER BY.
        # Build the filter as "prefix OR substring" — substring is the
        # superset, so this collapses to a single LIKE on the broader
        # pattern.
        filter_clauses = [
            Transaction.org_id == org_id,
            Transaction.type == type_enum,
            Transaction.description.like(substr_pat, escape="\\"),
        ]
    else:
        prefix_rank_expr = literal(0)
        filter_clauses = [
            Transaction.org_id == org_id,
            Transaction.type == type_enum,
        ]

    desc_stmt = (
        select(
            Transaction.description.label("description"),
            func.count(Transaction.id).label("use_count"),
            func.max(Transaction.date).label("last_used"),
            prefix_rank_expr.label("prefix_rank"),
        )
        .where(*filter_clauses)
        .group_by(Transaction.description)
        .order_by(
            desc("prefix_rank"),
            desc("use_count"),
            desc("last_used"),
        )
        .limit(limit)
    )
    rows = (await db.execute(desc_stmt)).all()
    if not rows:
        return []

    descriptions = [r.description for r in rows]

    # ── Phase 2: pick the most-frequent category per description ────
    # One follow-up query keyed by the descriptions we just selected.
    # We deliberately avoid a window function so the path stays
    # portable to SQLite (used by router/service tests) and MySQL 8.
    cat_stmt = (
        select(
            Transaction.description.label("description"),
            Transaction.category_id.label("category_id"),
            func.count(Transaction.id).label("pair_count"),
            func.max(Transaction.date).label("pair_last_used"),
        )
        .where(
            Transaction.org_id == org_id,
            Transaction.type == type_enum,
            Transaction.description.in_(descriptions),
        )
        .group_by(Transaction.description, Transaction.category_id)
        .order_by(
            Transaction.description,
            desc("pair_count"),
            desc("pair_last_used"),
        )
    )
    cat_rows = (await db.execute(cat_stmt)).all()

    # First row per description (sorted by count desc, recency desc)
    # is the winning pair.
    top_category_by_desc: dict[str, int] = {}
    for r in cat_rows:
        if r.description not in top_category_by_desc:
            top_category_by_desc[r.description] = r.category_id

    # ── Phase 3: resolve category display names ─────────────────────
    category_ids = list({cid for cid in top_category_by_desc.values()})
    name_stmt = select(Category.id, Category.name).where(
        Category.org_id == org_id,
        Category.id.in_(category_ids),
    )
    cat_name_rows = (await db.execute(name_stmt)).all()
    category_name_by_id = {row.id: row.name for row in cat_name_rows}

    # ── Phase 4: shape the response ─────────────────────────────────
    # If a description's category was somehow soft-deleted or scoped
    # out of the org, skip it rather than crash — the schema requires
    # a non-null category_id + name.
    out: list[DescriptionSuggestion] = []
    for r in rows:
        cat_id = top_category_by_desc.get(r.description)
        if cat_id is None:
            continue
        cat_name = category_name_by_id.get(cat_id)
        if cat_name is None:
            continue
        last_used = r.last_used
        if isinstance(last_used, datetime.datetime):
            last_used = last_used.date()
        out.append(
            DescriptionSuggestion(
                description=r.description,
                category_id=cat_id,
                category_name=cat_name,
                use_count=int(r.use_count),
                last_used=last_used,
            )
        )
    return out
