"""Credit-card utilization source (TBD-170).

⚠ THIS SOURCE IS NOT SHAPED LIKE THE OTHER FOUR. Read this before editing.

1. **POINT-IN-TIME. There is no time dimension and there cannot be one.**
   Historical *balance* is reconstructible (``networth.py`` rebuilds it from
   ``opening_balance`` plus settled deltas), but historical *credit limit* is
   not: ``Account.credit_limit`` is a mutable scalar overwritten in place
   (``routers/accounts.py``), there is no snapshot table, and a limit change
   writes no audit row. A series would therefore compute
   ``historical_balance / TODAY's limit`` — a user who raised a limit from
   €2,000 to €10,000 in June would see the whole prior year retroactively
   restated to a fifth of what they lived through. That is a fabricated
   number, not a degraded one. Utilization-over-time needs a credit-limit
   history substrate first.

2. **It is a RATIO source, not an additive one.** For any group,
   ``utilization_pct = 100 * Σoutstanding / Σcredit_limit`` — a limit-weighted
   ratio of sums, NEVER an unweighted average of ratios. A €200 store card at
   100% and a €20,000 card at 5% average to 52.5%; the true combined figure is
   5.94%. The wrong answer is *plausible*, which makes it worse than an
   obviously wrong one. ``build_rows`` therefore computes the value in Python
   and ignores ``measure.agg`` for that field (the ``net_worth`` nominal
   precedent).

3. **``outstanding`` is published POSITIVE**, inverting the repo-wide
   "liabilities stored negative" convention, to match
   ``creditUtilization()`` in ``frontend/lib/credit.ts``. A user placing
   accounts ``sum_balance`` and this source's ``outstanding`` on one canvas
   sees the same card twice with opposite signs. That is exactly why the field
   is named ``outstanding`` and not ``balance``. It renders as an unsigned
   magnitude and must never be colour-coded.

4. **Currency is ALWAYS in the internal group key.** A percentage looks exempt
   from "never sum across currencies" because it is dimensionless. It is not:
   aggregating sums currency into *both* numerator and denominator, i.e. an
   implicit 1.00 FX rate — and because the output is a percent, the usual
   reflex never fires.

5. **Sort and limit happen in PYTHON**, because ``utilization_pct`` only exists
   after the division. Applying ``.limit()`` in SQL would keep an arbitrary N
   rows and the Python sort would then order the wrong ones — silently dropping
   the highest-utilization cards, which are the entire point of the report.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.reports.sources import register
from app.reports.sources.base import (
    ReportSource,
    SourceDimension,
    SourceFilter,
    SourceMeasure,
    validate_against_catalog,
)
from app.schemas.reports_query import (
    MAX_LIMIT,
    resolve_truncated_end,
    Aggregation,
    Dimension,
    FilterField,
    MeasureField,
    ReportsQuery,
)

_DIMENSIONS = [
    SourceDimension("account", "Card", "account"),
    SourceDimension("currency", "Currency", "currency"),
    SourceDimension("account_active", "Status", "boolean"),
    # NO month/week/day — see the module docstring, point 1.
]

# ⚠ ORDER IS LOAD-BEARING: ``setDataset`` resets a switched widget to
# ``measures[0]``, so the headline measure must come first.
_MEASURES = [
    SourceMeasure("utilization_pct", "Utilization", "avg", "utilization_pct", "percent"),
    SourceMeasure("outstanding", "Outstanding", "sum", "outstanding", "currency"),
    SourceMeasure("credit_limit", "Credit limit", "sum", "credit_limit", "currency"),
    SourceMeasure("count_cards", "Card count", "count", "id", "number"),
]

_FILTERS = [
    SourceFilter("account_id", "Card", ("in",), "account"),
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    SourceFilter("account_active", "Status", ("eq",), "boolean"),
    # NO date filter — the source is point-in-time. A stray shared-canvas date
    # is dropped by the SHARED_CANVAS_FILTER_FIELDS contract.
]

# The declared agg for every measure field. ⚠ EXHAUSTIVE on purpose: gating
# only ``utilization_pct`` would leave the same inversion one field over —
# ``avg(outstanding)`` passes the pydantic numeric gate AND
# ``validate_against_catalog`` (which checks the field, never the agg), and
# build_rows only ever SUMs, so two cards at €900 and €100 would render as
# "average outstanding = €1,000".
_DECLARED_AGG = {
    MeasureField.UTILIZATION_PCT: Aggregation.AVG,
    MeasureField.OUTSTANDING: Aggregation.SUM,
    MeasureField.CREDIT_LIMIT: Aggregation.SUM,
    MeasureField.ID: Aggregation.COUNT,
}

_DIM_EXPR = {
    Dimension.ACCOUNT: ("account", Account.name),
    Dimension.CURRENCY: ("currency", Account.currency),
    # Verbatim from accounts.py so the two sources cannot drift on the label
    # strings, and so we never depend on the driver returning a Python bool.
    Dimension.ACCOUNT_ACTIVE: (
        "account_active",
        case((Account.is_active.is_(True), "Active"), else_="Inactive"),
    ),
}

_OWN_FILTER_FIELDS = {
    FilterField.ACCOUNT_ID,
    FilterField.CURRENCY,
    FilterField.ACCOUNT_ACTIVE,
}

# max(0, -balance). ⚠ NEVER func.greatest: it is MySQL-only and the source
# tests build on sqlite+aiosqlite, so it fails the suite outright
# (measured: "no such function: greatest"). Same portable-CASE rule as
# transaction_service and every other clamp in this package.
_OUTSTANDING = case((Account.balance < 0, -Account.balance), else_=0)


def _apply_filter(stmt, f):
    """Compile one of this source's OWN filters into a WHERE clause.

    ⚠ Publishing a filter is not honouring it. ``validate_against_catalog``
    ACCEPTS a published field without applying it, so a source that omits this
    loop returns unfiltered rows with no error anywhere.
    """
    field = f.field
    op = f.op.value
    if field is FilterField.ACCOUNT_ID:
        if op == "in":
            return stmt.where(Account.id.in_([int(v) for v in f.value]))
        raise ValueError(f"credit_utilization: account_id does not support op {op!r}")
    if field is FilterField.CURRENCY:
        if op == "eq":
            return stmt.where(Account.currency == f.value)
        if op == "in":
            return stmt.where(Account.currency.in_(list(f.value)))
        raise ValueError(f"credit_utilization: currency does not support op {op!r}")
    if field is FilterField.ACCOUNT_ACTIVE:
        if op == "eq":
            return stmt.where(Account.is_active.is_(bool(f.value)))
        raise ValueError(
            f"credit_utilization: account_active does not support op {op!r}"
        )
    return stmt  # field this source doesn't own → drop (shared-canvas contract)


def _value_for(measure, outstanding: float, credit_limit: float, card_count: int) -> float:
    """Select this row's single ``value`` for the REQUESTED measure.

    ⚠ A ``QueryRow`` carries exactly one ``value``. A build_rows that only ever
    emits the utilization number returns it for *every* requested measure — a
    KPI asking for ``count_cards`` would render "3 cards" as ``42.7``.
    """
    field = measure.field
    if field is MeasureField.UTILIZATION_PCT:
        # Denominator cannot be 0 — the WHERE gates credit_limit > 0 — but
        # guard anyway rather than trust a predicate two functions away.
        return (100.0 * outstanding / credit_limit) if credit_limit else 0.0
    if field is MeasureField.OUTSTANDING:
        return outstanding
    if field is MeasureField.CREDIT_LIMIT:
        return credit_limit
    if field is MeasureField.ID:
        return float(card_count)
    # ⚠ Explicit raise, not a fallthrough. This mapping is total over the four
    # published fields today; a fifth added later must fail loudly rather than
    # silently inherit whichever branch happens to be last.
    raise ValueError(f"credit_utilization: unsupported measure field {field!r}")


class CreditUtilizationSource:
    key = "credit_utilization"
    label = "Credit utilization"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    def filters(self) -> list[SourceFilter]:
        return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)

        declared = _DECLARED_AGG.get(query.measure.field)
        if declared is not None and query.measure.agg is not declared:
            raise ValueError(
                f"source 'credit_utilization' measure "
                f"{query.measure.field.value!r} must use agg "
                f"{declared.value!r}, not {query.measure.agg.value!r}"
            )

        # ⚠ Checked HERE, not in build_rows. _run_source_query wraps only
        # validate() in its try; build_rows is called outside it, so raising
        # there turns user input into a 500 instead of a 422.
        sort = query.sort
        if sort is not None and sort.by.value == "dimension" and not query.dimensions:
            raise ValueError("sort.by='dimension' requires at least one dimension")

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], dict]:
        started = time.perf_counter()

        requested_keys = [_DIM_EXPR[d][0] for d in query.dimensions]
        dim_exprs: list[tuple[str, Any]] = [
            (_DIM_EXPR[d][0], _DIM_EXPR[d][1].label(_DIM_EXPR[d][0]))
            for d in query.dimensions
        ]
        # Currency ALWAYS joins the group key — but only once, or the key
        # carries a duplicate column when the user requested it explicitly.
        currency_requested = "currency" in requested_keys
        if not currency_requested:
            dim_exprs.append(("currency", Account.currency.label("currency")))

        base = (
            select(
                *[expr for _, expr in dim_exprs],
                func.coalesce(func.sum(_OUTSTANDING), 0).label("outstanding"),
                func.coalesce(func.sum(Account.credit_limit), 0).label("credit_limit"),
                func.count(Account.id).label("card_count"),
            )
            .select_from(Account)
            .join(AccountType, AccountType.id == Account.account_type_id)
            .where(
                Account.org_id == org_id,  # org-scope on accounts ONLY
                AccountType.slug == "credit_card",
                Account.credit_limit.isnot(None),
                Account.credit_limit > 0,
            )
        )

        for f in query.filters:
            if f.field in _OWN_FILTER_FIELDS:
                base = _apply_filter(base, f)
            # else: stray shared-canvas field (date, …) — silently dropped

        raw_group_cols = [_DIM_EXPR[d][1] for d in query.dimensions]
        if not currency_requested:
            raw_group_cols.append(Account.currency)
        base = base.group_by(*raw_group_cols)

        result = await db.execute(base)
        raw = result.mappings().all()

        def _f(v) -> float:
            return float(v) if v is not None else 0.0

        out_rows: list[dict] = []
        currencies: set[str] = set()
        for r in raw:
            currencies.add(r.get("currency"))
            d: dict[str, Any] = {k: r.get(k) for k in requested_keys}
            if not currency_requested:
                # Kept for now; dropped below iff the whole set is one currency.
                d["currency"] = r.get("currency")
            d["value"] = _value_for(
                query.measure,
                _f(r.get("outstanding")),
                _f(r.get("credit_limit")),
                int(r.get("card_count") or 0),
            )
            out_rows.append(d)

        # ── sort + slice, IN PYTHON ────────────────────────────────────────
        sort = query.sort
        descending = True
        sort_key_name = "value"
        if sort is not None:
            descending = sort.dir.value != "asc"
            if sort.by.value == "dimension":
                sort_key_name = requested_keys[0]  # validate() guarantees one
        # Deterministic tiebreaker so truncation on ties is stable across runs,
        # matching accounts.py's func.min(Account.id) intent.
        if sort_key_name == "value":
            out_rows.sort(
                key=lambda d: (
                    -d["value"] if descending else d["value"],
                    str(d.get("account") or ""),
                    str(d.get("currency") or ""),
                )
            )
        else:
            out_rows.sort(
                key=lambda d: (str(d.get(sort_key_name) or ""), str(d.get("currency") or "")),
                reverse=descending,
            )

        # ⚠ ``truncated`` is measured PRE-slice, on the unsliced in-memory
        # rows — "there was MORE than we returned". Do not "unify" this on
        # ``len(out_rows) >= limit`` after the slice: that is true for every
        # complete result that exactly fills the limit (TBD-484).
        limit = min(query.limit, MAX_LIMIT)
        truncated = len(out_rows) > limit
        out_rows = out_rows[:limit]

        # Single currency → drop the key we carried for partitioning only.
        multi_currency = len({c for c in currencies if c is not None}) > 1
        if not currency_requested and not multi_currency:
            for d in out_rows:
                d.pop("currency", None)

        # ── excluded-card disclosure ──────────────────────────────────────
        # Silent exclusion is not acceptable: a limitless card is a real card
        # the user owns, and Reports has no "No limit set" side-list the way
        # the dashboard tile does.
        excluded = (
            await db.scalar(
                select(func.count())
                .select_from(Account)
                .join(AccountType, AccountType.id == Account.account_type_id)
                .where(
                    Account.org_id == org_id,
                    AccountType.slug == "credit_card",
                    (Account.credit_limit.is_(None)) | (Account.credit_limit <= 0),
                )
            )
        ) or 0

        # ⚠ QueryMeta.warning is a single Optional[str]. Both notices can apply
        # to one query, so compose — a second assignment would silently discard
        # the first and tell the user about only one of them.
        notices: list[str] = []
        if multi_currency:
            notices.append(
                "This organization holds credit cards in more than one "
                "currency; currencies are never summed, so rows stay "
                "partitioned by currency."
            )
        if excluded:
            notices.append(
                f"{excluded} credit card(s) excluded — no credit limit set."
            )

        meta = {
            "row_count": len(out_rows),
            "truncated": truncated,
            # ⚠ Derived from the IN-PYTHON sort above (same semantics as the
            # SQL sources: value DESC keeps the top, so the tail is what
            # went). This source publishes no time dimension either, so a
            # by-dimension sort resolves to None rather than a guess.
            "truncated_end": (
                resolve_truncated_end(query.sort, query.dimensions)
                if truncated
                else None
            ),
            "query_ms": int((time.perf_counter() - started) * 1000),
        }
        if notices:
            meta["warning"] = " ".join(notices)
        return out_rows, meta


_INSTANCE: ReportSource = CreditUtilizationSource()
register(_INSTANCE)
