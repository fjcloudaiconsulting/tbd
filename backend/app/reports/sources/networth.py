"""Net-worth source — cumulative net worth OVER TIME, per currency.

Reports v3 Phase 6 (Group A). Snapshot net worth is already expressible via
the Accounts source (``sum(balance)``, no dimension), so this source exists
for the TIME-SERIES: what Σ (signed) balance *was* at the end of each past
period, reconstructed from ``opening_balance`` + signed settled deltas.

Design & rationale: ``specs/2026-07-26-reports-v3-phase6-networth-source-design.md``.

Load-bearing choices:
  * ``accounts.balance`` already stores liabilities NEGATIVE, so net worth =
    Σ signed balance per currency — no asset/liability classification needed.
  * Reconstruct with ``balance_contribution_filter()`` (NOT the reportable
    filter): it is the exact set for which ``Σ signed(settled rows) ==
    balance − opening_balance``. It KEEPS transfer legs (they net to zero
    within a currency) and manual adjustments (real balance moves), and DROPS
    reconcile-matched dupes / skipped / rejected rows. Status is gated
    separately (``WHERE status = SETTLED``).
  * NEVER sum across currencies (no FX). Always partition by currency; a
    multi-currency org with no ``currency`` dimension gets per-currency series
    + a ``meta.warning``.
  * The ``date`` filter is a display WINDOW, not a row filter: the upper bound
    is the reconstruction cutoff; the lower bound only slices output periods
    (the running total entering the first visible period carries full history).
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date as date_cls
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.reports.sources import register
from app.reports.sources.base import (
    ReportSource, SourceDimension, SourceFilter, SourceMeasure,
    validate_against_catalog,
)
from app.schemas.reports_query import (
    MAX_LIMIT,
    Dimension,
    FilterField,
    FilterOp,
    ReportsQuery,
)
from app.services.reports_query_service import QUERY_TIMEOUT_MS
from app.services.transaction_filters import (
    balance_contribution_filter,
    effective_period_date_expr,
)

_TIME_DIMS = {Dimension.MONTH, Dimension.WEEK, Dimension.DAY}
_TIME_KEY = {Dimension.MONTH: "month", Dimension.WEEK: "week", Dimension.DAY: "day"}

_DIMENSIONS = [
    SourceDimension("month", "Month", "time"),
    SourceDimension("week", "Week", "time"),
    SourceDimension("day", "Day", "time"),
    SourceDimension("currency", "Currency", "currency"),
]

# One nominal measure. build_rows ignores agg/field and always computes the
# cumulative reconstruction; the field ``net_worth`` exists so the frontend
# labels the axis/tooltip/CSV "Net worth" (it labels by field, not measure key).
_MEASURES = [
    SourceMeasure("net_worth", "Net worth", "sum", "net_worth", "currency"),
]

_FILTERS = [
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    SourceFilter("account_id", "Account", ("in",), "account"),
    # ``date`` ops mirror transactions.py so a start-only / end-only / range
    # from the shared canvas bar validates instead of 422-ing. Interpreted as
    # a display window (see module docstring + build_rows).
    SourceFilter("date", "Date", ("between", "gte", "lte"), "time"),
]

_MYSQL_TIMEOUT_HINT = f"/*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_MS}) */"


def _bucket_sql(date_col, granularity: Dimension, dialect_name: str):
    """Dialect-aware period-bucket over an ARBITRARY date column.

    ``reports_query_service._dimension_expr`` is hard-wired to the tx effective
    date, so it can't bucket Stream 1's ``opening_balance_date``. This mirrors
    its strftime/date_format switch, parameterized by the column.
    """
    if granularity is Dimension.MONTH:
        return func.strftime("%Y-%m", date_col) if dialect_name == "sqlite" else func.date_format(date_col, "%Y-%m")
    if granularity is Dimension.WEEK:
        return func.strftime("%Y-%W", date_col) if dialect_name == "sqlite" else func.date_format(date_col, "%x-%v")
    if granularity is Dimension.DAY:
        return func.strftime("%Y-%m-%d", date_col) if dialect_name == "sqlite" else func.date_format(date_col, "%Y-%m-%d")
    raise ValueError(f"networth: unsupported time granularity {granularity!r}")


def _bucket_py(d: date_cls, granularity: Dimension, dialect_name: str) -> str:
    """Python equivalent of ``_bucket_sql`` (same dialect format) so the lower
    date bound can be sliced against the SQL-produced period strings. WEEK
    differs between MySQL (ISO ``%x-%v``) and SQLite (``%Y-%W``); month/day
    are identical across both and Python."""
    if granularity is Dimension.MONTH:
        return d.strftime("%Y-%m")
    if granularity is Dimension.DAY:
        return d.strftime("%Y-%m-%d")
    if granularity is Dimension.WEEK:
        if dialect_name == "sqlite":
            return d.strftime("%Y-%W")
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-{iso_week:02d}"
    raise ValueError(f"networth: unsupported time granularity {granularity!r}")


def _signed_delta():
    """+amount for INCOME, −amount for EXPENSE. Transfer legs are typed
    INCOME/EXPENSE by direction (TransactionType.TRANSFER is reserved/unused on
    legs), so this signs them correctly and the two legs cancel within a
    currency."""
    return func.sum(
        case((Transaction.type == TransactionType.INCOME, Transaction.amount), else_=-Transaction.amount)
    )


def _as_float(value) -> float:
    return float(value) if value is not None else 0.0


class NetWorthSource:
    key = "networth"
    label = "Net worth"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    def filters(self) -> list[SourceFilter]:
        return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]:
        started = time.perf_counter()
        try:
            dialect = db.get_bind().dialect.name
        except Exception:  # pragma: no cover - defensive
            dialect = "mysql"

        time_dim: Optional[Dimension] = next(
            (d for d in query.dimensions if d in _TIME_DIMS), None
        )
        currency_requested = Dimension.CURRENCY in query.dimensions

        # ── parse filters (window + true data filters); stray shared-canvas
        #    fields (category_id, status, account_id-when-not-published…) drop ──
        lo: Optional[date_cls] = None
        hi: Optional[date_cls] = None
        currency_vals: Optional[list] = None
        account_ids: Optional[list] = None
        for f in query.filters:
            if f.field is FilterField.DATE:
                if f.op is FilterOp.BETWEEN:
                    lo, hi = f.value
                elif f.op is FilterOp.GTE:
                    lo = f.value
                elif f.op is FilterOp.LTE:
                    hi = f.value
            elif f.field is FilterField.CURRENCY:
                currency_vals = list(f.value) if f.op is FilterOp.IN else [f.value]
            elif f.field is FilterField.ACCOUNT_ID:
                if f.op is FilterOp.IN:
                    account_ids = list(f.value)

        # ── Stream 1: opening events (over accounts) ──
        open_stmt = select(Account.currency.label("currency")).where(Account.org_id == org_id)
        if time_dim is not None:
            period = _bucket_sql(Account.opening_balance_date, time_dim, dialect)
            open_stmt = select(
                period.label("period"),
                Account.currency.label("currency"),
                func.sum(Account.opening_balance).label("delta"),
            ).where(Account.org_id == org_id).group_by(period, Account.currency)
        else:
            open_stmt = select(
                Account.currency.label("currency"),
                func.sum(Account.opening_balance).label("delta"),
            ).where(Account.org_id == org_id).group_by(Account.currency)
        if currency_vals is not None:
            open_stmt = open_stmt.where(Account.currency.in_(currency_vals))
        if account_ids is not None:
            open_stmt = open_stmt.where(Account.id.in_(account_ids))
        if hi is not None:
            open_stmt = open_stmt.where(Account.opening_balance_date <= hi)

        # ── Stream 2: settled balance-contributing tx deltas (tx JOIN accounts) ──
        eff = effective_period_date_expr()
        base_where = [
            Transaction.org_id == org_id,
            Account.org_id == org_id,  # defense-in-depth alongside the tx org gate
            Transaction.status == TransactionStatus.SETTLED,
            balance_contribution_filter(),
        ]
        if time_dim is not None:
            period = _bucket_sql(eff, time_dim, dialect)
            delta_stmt = select(
                period.label("period"),
                Account.currency.label("currency"),
                _signed_delta().label("delta"),
            ).select_from(Transaction).join(Account, Account.id == Transaction.account_id).where(*base_where).group_by(period, Account.currency)
        else:
            delta_stmt = select(
                Account.currency.label("currency"),
                _signed_delta().label("delta"),
            ).select_from(Transaction).join(Account, Account.id == Transaction.account_id).where(*base_where).group_by(Account.currency)
        if currency_vals is not None:
            delta_stmt = delta_stmt.where(Account.currency.in_(currency_vals))
        if account_ids is not None:
            delta_stmt = delta_stmt.where(Transaction.account_id.in_(account_ids))
        if hi is not None:
            delta_stmt = delta_stmt.where(eff <= hi)

        if dialect == "mysql":
            open_stmt = open_stmt.prefix_with(_MYSQL_TIMEOUT_HINT, dialect="mysql")
            delta_stmt = delta_stmt.prefix_with(_MYSQL_TIMEOUT_HINT, dialect="mysql")

        open_rows = (await db.execute(open_stmt)).mappings().all()
        delta_rows = (await db.execute(delta_stmt)).mappings().all()

        # ── merge both streams into per-(currency, period) deltas, cumulate ──
        currencies_seen: set[str] = set()
        if time_dim is not None:
            deltas: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
            for r in list(open_rows) + list(delta_rows):
                cur = r["currency"]
                currencies_seen.add(cur)
                deltas[cur][r["period"]] += Decimal(str(r["delta"] or 0))
            lo_key = _bucket_py(lo, time_dim, dialect) if lo is not None else None
            key_name = _TIME_KEY[time_dim]
            rows: list[dict] = []
            for cur in sorted(deltas):
                running = Decimal(0)
                for per in sorted(deltas[cur]):
                    running += deltas[cur][per]
                    if lo_key is not None and per < lo_key:
                        continue  # accumulate history, but slice it out of output
                    rows.append({key_name: per, "currency": cur, "value": float(running)})
            rows.sort(key=lambda d: (d[key_name], d["currency"]))
        else:
            totals: dict[str, Decimal] = defaultdict(Decimal)
            for r in list(open_rows) + list(delta_rows):
                cur = r["currency"]
                currencies_seen.add(cur)
                totals[cur] += Decimal(str(r["delta"] or 0))
            rows = [
                {"currency": cur, "value": float(totals[cur])}
                for cur in sorted(totals)
            ]
            rows.sort(key=lambda d: (-d["value"], d["currency"]))

        multi_currency = len(currencies_seen) > 1
        # Drop the currency key when it wasn't requested AND the org is
        # single-currency (clean single series). Keep it (to never merge
        # currencies) when multi-currency, and warn.
        if not currency_requested and not multi_currency:
            for r in rows:
                r.pop("currency", None)

        # Truncate to the limit. A time series is sorted period-ASCENDING, so
        # keep the most-recent TAIL (the "today" end users want on a net-worth
        # chart), not the oldest head. The no-dimension / by-currency path is
        # value-desc, so its head is the right keep. truncated reflects whether
        # anything was actually dropped (measured pre-slice).
        limit = min(query.limit, MAX_LIMIT)
        total = len(rows)
        rows = rows[-limit:] if time_dim is not None else rows[:limit]

        meta: dict = {
            "row_count": len(rows),
            "truncated": total > limit,
            "query_ms": int((time.perf_counter() - started) * 1000),
        }
        if not currency_requested and multi_currency:
            meta["warning"] = "Multiple currencies held; showing per-currency net worth (currencies are never summed)."
        return rows, meta


_INSTANCE: ReportSource = NetWorthSource()
register(_INSTANCE)
