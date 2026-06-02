"""Shared response schemas reused across routers.

``ListEnvelope`` standardizes the shape every admin (and, going forward,
non-admin) list endpoint returns: ``{items, total, limit, offset}``. The
five admin tables (users, orgs, subscriptions, audit, rate-limit-overrides)
fan out from this single envelope so the frontend's shared ``useTableState``
+ ``Pagination`` components can read one contract everywhere.

``total`` is the COUNT over the *same filtered query* as ``items`` (minus
limit/offset), never the unfiltered table size. See ``app.services.list_query``
for the org-scoping contract that keeps ``total`` from leaking a cross-scope
count.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ListEnvelope(BaseModel, Generic[T]):
    """Generic paginated list response.

    ``items`` is the current page; ``total`` is the full filtered row count
    (so the client can compute page count); ``limit``/``offset`` echo the
    clamped values the server actually applied.
    """

    items: list[T]
    total: int
    limit: int
    offset: int
