"""Tiny time utilities — single home for the naive-UTC convention.

The repo's existing ``DateTime`` columns are declared without
timezone info (the SQLAlchemy default), and historical code used
``datetime.utcnow()`` to build matching naive-UTC values. Python
3.12 deprecated ``utcnow()`` because it returns a *naive* datetime
representing UTC, which is easy to confuse with local time.

``utcnow_naive()`` here computes ``datetime.now(timezone.utc)`` and
strips tzinfo at the boundary. Same wire result as ``utcnow()``,
no deprecation warning, and the function name records that the
result is intentionally naive.

A future repo-wide sweep can change the columns to
``DateTime(timezone=True)`` and remove the ``.replace(tzinfo=None)``
call site by site. For now this is the drop-in replacement.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Naive-UTC ``datetime`` matching the repo's column convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
