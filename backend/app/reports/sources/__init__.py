"""Source registry. Concrete sources self-register on import."""
from __future__ import annotations

from app.reports.sources.base import ReportSource

_REGISTRY: dict[str, ReportSource] = {}


def register(source: ReportSource) -> None:
    if source.key in _REGISTRY:
        raise ValueError(f"duplicate report source key: {source.key!r}")
    _REGISTRY[source.key] = source


def get_source(key: str) -> ReportSource:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"unknown report source: {key!r}") from exc


def all_sources() -> list[ReportSource]:
    return list(_REGISTRY.values())


# Import concrete sources so they self-register. Kept at the bottom to
# avoid a circular import (transactions.py imports from .base + __init__).
from app.reports.sources import transactions as _transactions  # noqa: E402,F401
