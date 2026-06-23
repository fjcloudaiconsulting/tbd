"""Reports v2 — AST → SQLAlchemy Core compiler + executor.

The architect-locked rule (spec §6 "Security model"):

- The AST is closed and validated by Pydantic before this module sees it.
- Compilation only consults whitelist tables on Transaction / Category /
  Account / Tag — never user-supplied strings.
- ``org_id`` is INJECTED here; the AST has no way to express it.
- All values reach the database as bound parameters (SQLAlchemy
  ``where()`` + ``bindparam`` semantics). No string interpolation.

Tag-filter semantics mirror the transactions list endpoint at
``backend/app/routers/transactions.py:90`` + the implementation at
``backend/app/services/transaction_service.py:1697``. ``tag_match=all``
(default) AND-combines every named tag; ``tag_match=any`` OR-combines.
"""
from __future__ import annotations

import time
from typing import Any, Tuple

from sqlalchemy import (
    Select,
    and_,
    distinct,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.tag import Tag, TransactionTag
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.reports_query import (
    MAX_LIMIT,
    Aggregation,
    Dimension,
    Filter,
    FilterField,
    FilterOp,
    Measure,
    MeasureField,
    ReportsQuery,
    TagMatch,
)
from app.services.transaction_filters import effective_period_date_expr


# Mapping: schema Dimension → (column expression factory, key string used
# in the response row). The factories take ``dialect_name`` so the time
# bucket dimensions can swap between MySQL ``DATE_FORMAT`` and SQLite
# ``strftime`` without leaking dialect logic into the router.


_DIM_KEYS: dict[Dimension, str] = {
    Dimension.CATEGORY: "category",
    Dimension.CATEGORY_MASTER: "category_master",
    Dimension.ACCOUNT: "account",
    Dimension.TAG: "tag",
    Dimension.TXN_TYPE: "txn_type",
    Dimension.STATUS: "status",
    Dimension.MONTH: "month",
    Dimension.WEEK: "week",
    Dimension.DAY: "day",
}


def _dimension_expr(dim: Dimension, dialect_name: str):
    """Return the SQLAlchemy expression for a dimension column.

    Time-bucket dimensions use MySQL ``DATE_FORMAT`` in production and
    SQLite ``strftime`` under pytest. This keeps the compiler dialect-
    agnostic from the AST perspective.
    """
    if dim is Dimension.CATEGORY:
        return Category.name
    if dim is Dimension.CATEGORY_MASTER:
        # Parent category name when the row's category has a parent;
        # else the category's own name. Computed via COALESCE on
        # parent.name + own name. Joining the parent table happens in
        # the JOIN block below.
        return func.coalesce(
            _category_parent_name_label(), Category.name
        )
    if dim is Dimension.ACCOUNT:
        return Account.name
    if dim is Dimension.TAG:
        return Tag.name
    if dim is Dimension.TXN_TYPE:
        return Transaction.type
    if dim is Dimension.STATUS:
        return Transaction.status
    # Time-bucket dimensions. Cash-basis: bucket by the effective settled
    # date (coalesce(settled_date, date)) so a row dated in month X but
    # settled in month Y is counted in Y, consistent with the list/forecast.
    eff = effective_period_date_expr()
    if dim is Dimension.MONTH:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m", eff)
        return func.date_format(eff, "%Y-%m")
    if dim is Dimension.WEEK:
        if dialect_name == "sqlite":
            # Year + ISO week. SQLite's strftime("%W") is "week of year
            # zero-padded (00..53)" — close enough for the AST contract,
            # mirrors MySQL's WEEK() default mode.
            return func.strftime("%Y-%W", eff)
        return func.date_format(eff, "%x-%v")
    if dim is Dimension.DAY:
        if dialect_name == "sqlite":
            return func.strftime("%Y-%m-%d", eff)
        return func.date_format(eff, "%Y-%m-%d")
    raise ValueError(f"unsupported dimension {dim!r}")


_PARENT_CATEGORY_ALIAS = None


def _category_parent_name_label():
    """Return a column expression that evaluates to the parent category
    name. We lazily create an alias on Category so the JOIN block can
    reference it. The alias is reused across the compiler.
    """
    global _PARENT_CATEGORY_ALIAS
    if _PARENT_CATEGORY_ALIAS is None:
        from sqlalchemy.orm import aliased

        _PARENT_CATEGORY_ALIAS = aliased(Category, name="category_master")
    return _PARENT_CATEGORY_ALIAS.name


def _category_parent_alias():
    """Return the aliased Category used as the parent-side join.
    Created lazily; the dimension expression and the JOIN block both
    consult this so they stay in sync.
    """
    global _PARENT_CATEGORY_ALIAS
    if _PARENT_CATEGORY_ALIAS is None:
        from sqlalchemy.orm import aliased

        _PARENT_CATEGORY_ALIAS = aliased(Category, name="category_master")
    return _PARENT_CATEGORY_ALIAS


_FILTER_COLUMN: dict[FilterField, Any] = {
    # Cash-basis: the DATE filter compares against the effective settled
    # date (coalesce(settled_date, date)), so a date-window filter buckets
    # rows by when they settled — consistent with the time dimensions.
    FilterField.DATE: effective_period_date_expr(),
    FilterField.AMOUNT: Transaction.amount,
    FilterField.CATEGORY_ID: Transaction.category_id,
    FilterField.ACCOUNT_ID: Transaction.account_id,
    FilterField.TXN_TYPE: Transaction.type,
    FilterField.STATUS: Transaction.status,
    # TAG_NAME is handled separately because it needs a join +
    # subquery (mirrors the transactions list semantics).
}


def _coerce_enum_value(field: FilterField, value):
    """Coerce a wire string into the SQLAlchemy enum value where
    necessary so the comparison binds correctly. Decimal / int / date
    pass through unchanged.
    """
    if field is FilterField.TXN_TYPE:
        return TransactionType(value)
    if field is FilterField.STATUS:
        return TransactionStatus(value)
    return value


def _apply_scalar_filter(stmt: Select, f: Filter) -> Select:
    col = _FILTER_COLUMN[f.field]
    op = f.op
    if op is FilterOp.EQ:
        return stmt.where(col == _coerce_enum_value(f.field, f.value))
    if op is FilterOp.GTE:
        return stmt.where(col >= _coerce_enum_value(f.field, f.value))
    if op is FilterOp.LTE:
        return stmt.where(col <= _coerce_enum_value(f.field, f.value))
    if op is FilterOp.IN:
        values = [_coerce_enum_value(f.field, v) for v in f.value]
        return stmt.where(col.in_(values))
    if op is FilterOp.BETWEEN:
        lo, hi = f.value
        return stmt.where(col.between(lo, hi))
    raise ValueError(f"unsupported op {op!r}")


def _apply_tag_filter(stmt: Select, f: Filter, org_id: int) -> Select:
    """Tag filter — mirrors transactions list semantics.

    ``tag_match=all``: each named tag yields its own IN subquery,
    AND-combined via repeated ``stmt.where(...)``.
    ``tag_match=any``: a single IN subquery against the union of
    named tags, OR-combined.

    Reference: ``backend/app/services/transaction_service.py:1697``.
    """
    # Coerce to a list of normalized names.
    if f.op is FilterOp.IN:
        names = [str(v).strip().lower() for v in f.value if str(v).strip()]
    elif f.op is FilterOp.EQ:
        names = [str(f.value).strip().lower()]
    else:
        raise ValueError(
            f"tag_name filter only supports op='eq' / op='in'; got {f.op!r}"
        )
    if not names:
        return stmt

    if f.tag_match is TagMatch.ANY:
        return stmt.where(
            Transaction.id.in_(
                select(TransactionTag.transaction_id)
                .join(Tag, Tag.id == TransactionTag.tag_id)
                .where(
                    Tag.org_id == org_id,
                    Tag.name_normalized.in_(names),
                )
            )
        )
    # ALL semantics: one IN-subquery per name, AND-combined.
    for name in names:
        stmt = stmt.where(
            Transaction.id.in_(
                select(TransactionTag.transaction_id)
                .join(Tag, Tag.id == TransactionTag.tag_id)
                .where(
                    Tag.org_id == org_id,
                    Tag.name_normalized == name,
                )
            )
        )
    return stmt


def _measure_expr(measure: Measure):
    """Translate a Measure into a SQLAlchemy aggregate expression."""
    agg = measure.agg
    field = measure.field
    col_map = {
        MeasureField.AMOUNT: Transaction.amount,
        MeasureField.ID: Transaction.id,
        MeasureField.CATEGORY_ID: Transaction.category_id,
        MeasureField.ACCOUNT_ID: Transaction.account_id,
    }
    col = col_map[field]
    if agg is Aggregation.SUM:
        return func.coalesce(func.sum(col), 0).label("value")
    if agg is Aggregation.COUNT:
        return func.count(col).label("value")
    if agg is Aggregation.AVG:
        return func.coalesce(func.avg(col), 0).label("value")
    if agg is Aggregation.DISTINCT:
        return func.count(distinct(col)).label("value")
    raise ValueError(f"unsupported aggregation {agg!r}")


def compile_ast_to_query(
    ast: ReportsQuery,
    *,
    org_id: int,
    dialect_name: str = "mysql",
) -> Select:
    """Compile a validated ``ReportsQuery`` AST into a SQLAlchemy Core
    ``Select`` bound to a single org.

    The AST has no way to specify ``org_id``; it is injected here from
    the authenticated caller's context. All other values reach the
    statement as bound parameters (SQLAlchemy's default).
    """
    # 1) Measure projection + base select.
    measure_expr = _measure_expr(ast.measure)
    dim_exprs: list[tuple[str, Any]] = []
    for dim in ast.dimensions:
        key = _DIM_KEYS[dim]
        expr = _dimension_expr(dim, dialect_name).label(key)
        dim_exprs.append((key, expr))

    stmt = select(*[expr for _, expr in dim_exprs], measure_expr).select_from(
        Transaction
    )

    # 2) Joins driven by dimension requirements.
    requested = {dim for dim in ast.dimensions}
    if Dimension.CATEGORY in requested or Dimension.CATEGORY_MASTER in requested:
        stmt = stmt.join(Category, Category.id == Transaction.category_id)
    if Dimension.CATEGORY_MASTER in requested:
        parent = _category_parent_alias()
        stmt = stmt.outerjoin(parent, parent.id == Category.parent_id)
    if Dimension.ACCOUNT in requested:
        stmt = stmt.join(Account, Account.id == Transaction.account_id)
    if Dimension.TAG in requested:
        stmt = stmt.join(
            TransactionTag, TransactionTag.transaction_id == Transaction.id
        ).join(Tag, Tag.id == TransactionTag.tag_id)

    # 3) Filters. ``org_id`` is the load-bearing WHERE; appended unconditionally.
    stmt = stmt.where(Transaction.org_id == org_id)
    for f in ast.filters:
        if f.field is FilterField.TAG_NAME:
            stmt = _apply_tag_filter(stmt, f, org_id)
        else:
            stmt = _apply_scalar_filter(stmt, f)

    # 4) GROUP BY (only when there are dimensions).
    if dim_exprs:
        # Group by the raw (unlabeled) expressions; aliased labels can
        # confuse some MySQL versions inside GROUP BY.
        raw_group_cols = []
        for dim in ast.dimensions:
            raw_group_cols.append(_dimension_expr(dim, dialect_name))
        stmt = stmt.group_by(*raw_group_cols)

    # 5) ORDER BY.
    sort = ast.sort
    if sort is not None:
        if sort.by.value == "value":
            order_col = literal_column("value")
        else:
            # Sort by the first dimension column when ``dimension`` is
            # requested (no AST way to pick a specific dim yet — spec
            # §6 keeps the surface narrow).
            if not dim_exprs:
                raise ValueError(
                    "sort.by='dimension' requires at least one dimension"
                )
            order_col = literal_column(dim_exprs[0][0])
        if sort.dir.value == "asc":
            stmt = stmt.order_by(order_col.asc())
        else:
            stmt = stmt.order_by(order_col.desc())
    elif dim_exprs:
        # Stable default order by value desc.
        stmt = stmt.order_by(literal_column("value").desc())

    # 6) Hard limit cap. Pydantic already enforced ``limit <= 500`` on
    # the AST; the ``min()`` is defence-in-depth in case the AST is ever
    # constructed in Python without going through validation.
    stmt = stmt.limit(min(ast.limit, MAX_LIMIT))
    return stmt


# ─── execution helpers ──────────────────────────────────────────────


# Spec §6 "Hard caps" — "Per-request query timeout 5 s". MySQL 8.0
# supports the ``MAX_EXECUTION_TIME(ms)`` optimizer hint inside the
# statement, so we attach it as a SELECT prefix at compile time. The
# hint binds the limit to THIS statement only (not the connection or
# the pool), so a long-running report can't poison the next request.
#
# Under SQLite (pytest) the hint is meaningless: SQLite ignores
# unrecognized comment-form hints, but ``prefix_with`` also injects it
# OUTSIDE the comment markers. To keep the test backend happy we only
# attach the hint when the dialect is MySQL.
QUERY_TIMEOUT_MS = 5000
_MYSQL_TIMEOUT_HINT = f"/*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */"


def _apply_query_timeout(stmt: Select, dialect_name: str) -> Select:
    """Attach a per-statement timeout to the compiled SELECT.

    MySQL: inject the ``MAX_EXECUTION_TIME`` optimizer hint via
    ``prefix_with`` so it lands right after the ``SELECT`` keyword. The
    server aborts the query (and only the query — not the connection)
    once the wall-clock exceeds ``QUERY_TIMEOUT_MS``.

    SQLite (pytest harness): no-op. SQLite does not understand the hint
    syntax and tests don't need the cap. The router still relies on the
    Pydantic-validated AST + ``MAX_LIMIT`` to keep test queries small.
    """
    if dialect_name == "mysql":
        return stmt.prefix_with(_MYSQL_TIMEOUT_HINT, dialect="mysql")
    return stmt


async def execute_query(
    db: AsyncSession,
    ast: ReportsQuery,
    *,
    org_id: int,
) -> Tuple[list[dict], dict]:
    """Execute a validated AST and return ``(rows, meta)``.

    Returns dicts keyed by dimension name plus ``"value"``. Caller
    serializes to ``ReportsQueryResponse``.

    Per-request query timeout (spec §6 "Hard caps"): on MySQL the
    compiled SELECT carries a ``MAX_EXECUTION_TIME(5000)`` optimizer
    hint so the server aborts a runaway aggregation after 5 s without
    poisoning the connection. SQLite (the pytest backend) does not
    understand the hint and silently skips it; the small fixture
    datasets used in tests finish in milliseconds, so the gap is
    intentional rather than a defect.
    """
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        dialect = "mysql"
    stmt = compile_ast_to_query(ast, org_id=org_id, dialect_name=dialect)
    stmt = _apply_query_timeout(stmt, dialect)
    started = time.perf_counter()
    result = await db.execute(stmt)
    rows = result.mappings().all()
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    out_rows = []
    for r in rows:
        d = {}
        for key, _ in [
            (_DIM_KEYS[dim], None) for dim in ast.dimensions
        ]:
            d[key] = r.get(key)
        # Coerce SUM(amount) Decimal → float for JSON.
        val = r.get("value")
        if hasattr(val, "as_tuple"):  # Decimal-like
            try:
                d["value"] = float(val)
            except Exception:  # pragma: no cover - defensive
                d["value"] = str(val)
        else:
            d["value"] = val
        out_rows.append(d)

    meta = {
        "row_count": len(out_rows),
        "truncated": len(out_rows) >= ast.limit,
        "query_ms": elapsed_ms,
    }
    return out_rows, meta
