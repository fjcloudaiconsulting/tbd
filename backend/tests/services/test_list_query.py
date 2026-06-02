"""Unit coverage for the shared list-contract helpers in
``app.services.list_query``.

Pure unit tests: ``resolve_order_by`` is asserted against a tiny
in-module SQLAlchemy model. Rather than reaching into SQLAlchemy
ordering-element internals (``.modifier.__name__`` etc.), each case
compiles ``select(...).order_by(*exprs)`` to SQL and asserts the
ORDER BY clause — the observable contract callers actually depend on.

Pagination clamping is the router's job (FastAPI ``Query(ge=..., le=...)``),
so it has no helper here and nothing to unit-test in this module.
"""
from __future__ import annotations

import pytest
from sqlalchemy import Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.services.exceptions import ValidationError
from app.services.list_query import resolve_order_by


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "list_query_test_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))


def _order_by_sql(exprs: list) -> str:
    """Compile ``ORDER BY *exprs`` to a literal SQL string for assertions."""
    stmt = select(_Row).order_by(*exprs)
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    # Return everything after ORDER BY, upper-cased for direction checks.
    return sql[sql.upper().index("ORDER BY"):].upper()


def _allowed():
    return {"created_at": _Row.id, "email": _Row.email, "name": _Row.name}


# ── resolve_order_by ──────────────────────────────────────────────────


def test_resolve_order_by_default_when_none():
    exprs = resolve_order_by(
        None, None, allowed=_allowed(), default_key="created_at", default_dir="desc"
    )
    assert len(exprs) == 1
    order_by = _order_by_sql(exprs)
    # Default key resolves to the mapped column (id), descending.
    assert "ID DESC" in order_by


def test_resolve_order_by_empty_string_uses_default():
    exprs = resolve_order_by(
        "", "", allowed=_allowed(), default_key="created_at", default_dir="desc"
    )
    assert "ID DESC" in _order_by_sql(exprs)


def test_resolve_order_by_valid_key_asc():
    exprs = resolve_order_by(
        "email", "asc", allowed=_allowed(), default_key="created_at"
    )
    assert "EMAIL ASC" in _order_by_sql(exprs)


def test_resolve_order_by_valid_key_desc():
    exprs = resolve_order_by(
        "email", "desc", allowed=_allowed(), default_key="created_at"
    )
    assert "EMAIL DESC" in _order_by_sql(exprs)


def test_resolve_order_by_unknown_key_raises():
    with pytest.raises(ValidationError) as exc:
        resolve_order_by(
            "not_a_column", "asc", allowed=_allowed(), default_key="created_at"
        )
    # Bare code only — the offending token is never echoed back.
    assert str(exc.value.detail) == "invalid_sort_by"


def test_resolve_order_by_invalid_dir_raises():
    with pytest.raises(ValidationError) as exc:
        resolve_order_by(
            "email", "sideways", allowed=_allowed(), default_key="created_at"
        )
    assert str(exc.value.detail) == "invalid_sort_dir"


def test_resolve_order_by_missing_dir_uses_default():
    exprs = resolve_order_by(
        "email", None, allowed=_allowed(), default_key="created_at", default_dir="asc"
    )
    assert "EMAIL ASC" in _order_by_sql(exprs)


def test_resolve_order_by_appends_tiebreaker():
    exprs = resolve_order_by(
        "email",
        "asc",
        allowed=_allowed(),
        default_key="created_at",
        tiebreaker=_Row.id.desc(),
    )
    assert len(exprs) == 2
    order_by = _order_by_sql(exprs)
    # Primary sort then the tiebreaker, in that order.
    assert order_by.index("EMAIL ASC") < order_by.index("ID DESC")


def test_resolve_order_by_no_tiebreaker_single_expr():
    exprs = resolve_order_by(
        "email", "asc", allowed=_allowed(), default_key="created_at"
    )
    assert len(exprs) == 1
