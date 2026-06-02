"""Shared, reusable helpers for the standard list contract (sort +
pagination) across every admin table — and, going forward, every
paginated list endpoint.

The frontend shipped the client half (``useTableState``, ``Pagination``,
``SortableHeader``). This module is the server half: it owns the
sort-whitelist (``resolve_order_by``) — resolving a *closed-whitelist*
sort into SQLAlchemy ordering expressions — and documents the
org-scoping contract below. Admin Users is the canonical reference
caller; orgs / subscriptions / audit / rate-limit-overrides fan out
from here.

Pagination clamping is NOT this module's job: it belongs to the router,
which clamps via FastAPI ``Query(ge=..., le=...)`` (e.g.
``limit: int = Query(50, ge=1, le=200)`` / ``offset: int = Query(0, ge=0)``).
That is the idiomatic boundary clamp and the single source of the
default/min/max, so every fan-out endpoint stays consistent without a
second clamp helper drifting out of sync.

Org-scoping contract (READ THIS BEFORE WIRING A NEW CALLER):

    Callers MUST apply the *same* tenant/filter WHERE clauses to BOTH the
    COUNT query and the page query. ``total`` is meaningless — worse, a
    cross-scope leak — if the count is computed over a wider set than the
    page. The pattern is: build ``where_clauses`` once, then loop applying
    each clause to ``base`` (the page query) and ``count_base`` (the
    ``select(func.count())``) before executing either. See
    ``admin_users_search_service.list_users`` for the reference shape.

Sort discipline mirrors the reports AST: an unknown ``sort_by`` is a
400, never a silent fallback. Silent fallback would (a) hide frontend
bugs and (b) open arbitrary-column ordering. ``resolve_order_by`` raises
``ValidationError`` so routers map it to HTTP 400 the same way they map
any other service-layer ``ValidationError``.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.services.exceptions import ValidationError

_SortColumn = Union[InstrumentedAttribute, ColumnElement]
_VALID_DIRS = frozenset({"asc", "desc"})


def resolve_order_by(
    sort_by: Optional[str],
    sort_dir: Optional[str],
    *,
    allowed: dict[str, _SortColumn],
    default_key: str,
    default_dir: str = "desc",
    tiebreaker: Optional[Any] = None,
) -> list:
    """Resolve ``sort_by`` / ``sort_dir`` into SQLAlchemy ordering exprs.

    ``allowed`` is the closed whitelist mapping public sort keys to the
    column (or column expression) to order by. ``default_key`` MUST be a
    key in ``allowed``.

    Behaviour:

    - ``sort_by`` None/empty   → use ``default_key``.
    - ``sort_by`` not in allowed → raise ``ValidationError("invalid_sort_by")``
      (closed whitelist — never silently fall back). The offending token is
      NOT interpolated into the detail (avoids log/echo-injection smell; the
      frontend sends a closed set anyway).
    - ``sort_dir`` None/empty  → use ``default_dir``.
    - ``sort_dir`` present but not in {"asc","desc"} → raise
      ``ValidationError("invalid_sort_dir")``.

    ``tiebreaker``, when provided, must be a fully-formed ordering
    expression (e.g. ``Model.id.desc()``), unlike ``allowed`` values
    which are bare columns that this function wraps with ``.asc()``/
    ``.desc()``.

    Returns ``[resolved_column.asc()/.desc(), *([tiebreaker] if given)]``
    so callers can splat directly into ``.order_by(*exprs)``.
    """
    key = sort_by or default_key
    if key not in allowed:
        raise ValidationError("invalid_sort_by")

    direction = sort_dir or default_dir
    if direction not in _VALID_DIRS:
        raise ValidationError("invalid_sort_dir")

    column = allowed[key]
    ordered = column.desc() if direction == "desc" else column.asc()

    exprs: list = [ordered]
    if tiebreaker is not None:
        exprs.append(tiebreaker)
    return exprs
