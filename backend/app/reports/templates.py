"""Reports v2 — starter report templates (code fixtures).

Architect-locked decision (Reports v2 "Slice 1"): the three starter
templates are Python code fixtures, NOT DB seed rows. ``GET
/api/v1/reports/templates`` returns them; the frontend "Use template"
action POSTs the chosen ``layout_json`` / ``canvas_filters_json`` to the
existing ``POST /api/v1/reports`` create endpoint.

CRITICAL — shape contract:

The ``layout_json`` widgets and ``canvas_filters_json`` MUST match the
*implemented* frontend widget-config shapes in
``frontend/lib/reports/types.ts``, because the canvas renders these dicts
directly. A shape mismatch renders a blank widget. The shapes used here:

- Single-measure widgets (kpi, bar, pie, sparkline) carry
  ``config.measure`` (a ``Measure`` = ``{agg, field}``).
- Multi-series widgets (line, area, stacked_bar, table) carry
  ``config.measures`` (a list of ``{measure: {agg, field}, label?}``).
- Per-widget filters use the ``WidgetFilters`` shape (e.g.
  ``{"txn_type": "expense"}``), NOT the raw AST ``{field, op, value}``
  primitive.
- Dimensions are drawn from the closed ``Dimension`` union: only
  ``category`` / ``month`` / ``day`` are used below.
- ``canvas_filters_json.date_range`` is a ``CanvasDateRange`` =
  ``{start, end}`` of ABSOLUTE ISO dates. The implemented frontend does
  NOT store relative preset strings: ``DatePresetChips`` resolves a
  preset to an absolute window at click time ("the backend AST doesn't
  model relative ranges; freezing the absolute window at click time").
  We mirror that here, computing the windows relative to the current
  date so the starter range is sensible when the template is cloned.
"""
from __future__ import annotations

from datetime import date, timedelta


def _measure(agg: str, field: str = "amount") -> dict:
    """A single ``Measure`` (``agg(field)``)."""
    return {"agg": agg, "field": field}


def _series(agg: str, label: str, field: str = "amount") -> dict:
    """A single ``SeriesConfig`` entry for multi-series widgets."""
    return {"measure": _measure(agg, field), "label": label}


def _iso(d: date) -> str:
    return d.isoformat()


def _this_month_range(today: date) -> dict:
    """``{start, end}`` for the calendar month containing ``today``.

    Mirrors ``DatePresetChips.buildPresetRanges`` ``this_month``.
    """
    start = today.replace(day=1)
    # First day of next month, minus one day, is the last day of this month.
    if today.month == 12:
        first_next = date(today.year + 1, 1, 1)
    else:
        first_next = date(today.year, today.month + 1, 1)
    last = first_next - timedelta(days=1)
    return {"start": _iso(start), "end": _iso(last)}


def _last_12_months_range(today: date) -> dict:
    """``{start, end}`` for the trailing 12 months.

    Mirrors ``DatePresetChips.buildPresetRanges`` ``last_12_months``:
    start = first day of the same month one year ago, end = today.
    """
    start = date(today.year - 1, today.month, 1)
    return {"start": _iso(start), "end": _iso(today)}


def get_report_templates() -> list[dict]:
    """Build the starter report templates with fresh date windows.

    The ``canvas_filters_json.date_range`` windows are computed from
    ``date.today()`` at CALL TIME (not module import) so a long-running
    backend always serves a window relative to the current date. The
    endpoint calls this per request; "this month" / "trailing 12 months"
    therefore roll over with the calendar instead of staying frozen to
    the date the process booted.
    """
    today = date.today()
    this_month = _this_month_range(today)
    last_12_months = _last_12_months_range(today)

    return [
    {
        "key": "monthly_review",
        "name": "Monthly review",
        "description": (
            "Net, income, and expense at a glance for the current month, "
            "plus spend by category and a daily net trend."
        ),
        "canvas_filters_json": {"date_range": this_month},
        "layout_json": {
            "version": 1,
            "widgets": [
                {
                    "id": "mr-kpi-net",
                    "type": "kpi",
                    "title": "Net",
                    "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                    },
                },
                {
                    "id": "mr-kpi-income",
                    "type": "kpi",
                    "title": "Income",
                    "grid": {"x": 4, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                        "filters": {"txn_type": "income"},
                    },
                },
                {
                    "id": "mr-kpi-expense",
                    "type": "kpi",
                    "title": "Expense",
                    "grid": {"x": 8, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                        "filters": {"txn_type": "expense"},
                    },
                },
                {
                    "id": "mr-bar-category",
                    "type": "bar",
                    "title": "Spend by category",
                    "grid": {"x": 0, "y": 2, "w": 6, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                        "dimensions": ["category"],
                        "filters": {"txn_type": "expense"},
                        "sort": {"by": "value", "dir": "desc"},
                        "limit": 10,
                    },
                },
                {
                    "id": "mr-line-net-trend",
                    "type": "line",
                    "title": "Net trend (daily)",
                    "grid": {"x": 6, "y": 2, "w": 6, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Net")],
                        "dimensions": ["day"],
                    },
                },
            ],
        },
    },
    {
        "key": "cash_flow_trend",
        "name": "Cash flow trend",
        "description": (
            "Average monthly net over the trailing year and net by month."
        ),
        "canvas_filters_json": {"date_range": last_12_months},
        "layout_json": {
            "version": 1,
            "widgets": [
                {
                    "id": "cft-kpi-avg-net",
                    "type": "kpi",
                    "title": "Avg monthly net (12mo)",
                    "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("avg"),
                    },
                },
                {
                    "id": "cft-line-net-by-month",
                    "type": "line",
                    "title": "Net by month",
                    "grid": {"x": 0, "y": 2, "w": 12, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Net")],
                        "dimensions": ["month"],
                    },
                },
            ],
        },
    },
    {
        "key": "category_deep_dive",
        "name": "Category deep-dive",
        "description": (
            "Category share of spend, a top-categories table, and a "
            "stacked category-by-month breakdown."
        ),
        "canvas_filters_json": {"date_range": this_month},
        "layout_json": {
            "version": 1,
            "widgets": [
                {
                    "id": "cdd-pie-share",
                    "type": "pie",
                    "title": "Category share",
                    "grid": {"x": 0, "y": 0, "w": 6, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                        "dimensions": ["category"],
                        "filters": {"txn_type": "expense"},
                    },
                },
                {
                    "id": "cdd-table-top",
                    "type": "table",
                    "title": "Top categories",
                    "grid": {"x": 6, "y": 0, "w": 6, "h": 4},
                    # TBD-426. This panel carried NO ``txn_type`` filter while
                    # both of its canvas neighbours (``cdd-pie-share`` and
                    # ``cdd-stacked-by-month``) filter to expenses. It is
                    # titled "Top categories", ranks by ``sum(amount)`` desc,
                    # and sits in a canvas whose own description reads
                    # "Category share of SPEND" -- so it read as top spending
                    # categories and was not.
                    #
                    # The defect was invisible at the canvas's ``this_month``
                    # window, where rent outranks a monthly salary line. Widen
                    # the window and ``Paycheck/Salary`` takes the top row of
                    # an expense deep-dive. Measured live at 12 months:
                    # EUR 19,500 income heading the table while the pie beside
                    # it showed EUR 9,297 of spend.
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Amount")],
                        "dimensions": ["category"],
                        "filters": {"txn_type": "expense"},
                        "sort": {"by": "value", "dir": "desc"},
                        "limit": 20,
                    },
                },
                {
                    "id": "cdd-stacked-by-month",
                    "type": "stacked_bar",
                    "title": "Category by month",
                    "grid": {"x": 0, "y": 4, "w": 12, "h": 4},
                    # TBD-382 R9. The two-dimension grouping is the point of
                    # this panel: the frontend now stacks each month's bar by
                    # category instead of collapsing every category onto the
                    # month label. Two additions ship with that fix:
                    #
                    # - ``sort`` was ABSENT, so the compiler applied its
                    #   default ``ORDER BY value DESC`` over (month, category)
                    #   PAIRS. That is not a reading of a time axis in any
                    #   state, and it is what made the client-side primary cap
                    #   ambiguous. Changes no data.
                    # - ``limit`` was ABSENT. With two dimensions the wire
                    #   limit caps PAIRS, so the frontend raises the AST limit
                    #   to the ceiling and treats this number as a cap on
                    #   MONTH buckets. 12 is a no-op on any window holding
                    #   twelve months or fewer.
                    #
                    # ⚠ ``measures`` stays length 1. stacked_bar stacks by
                    # dimensions[1], never by measures: no pair of published
                    # measures on any source has a meaningful sum.
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Spend")],
                        "dimensions": ["month", "category"],
                        "sort": {"by": "dimension", "dir": "asc"},
                        "limit": 12,
                        # TBD-382. This panel is the canvas's only TIME-SERIES view, so it carries
                        # its OWN trailing-12-month window instead of inheriting the canvas's
                        # `this_month`. Grouping by month over a one-month window is structurally a
                        # ONE-BAR chart: it renders as a ~60px progress bar, not a chart, and cannot
                        # show the stacking this panel exists to demonstrate. Widening the CANVAS was
                        # rejected -- it silently redefines "Category share" and "Top categories" as
                        # twelve-month questions, and puts an INCOME row at the top of a spend canvas.
                        # The override is DECLARED, never silent: WidgetShell's filter-chip header
                        # renders the effective window and marks it "overrides canvas" in the accent
                        # register (isFieldOverridden). Do NOT also state the window in the title --
                        # a frozen string asserting a live filter value is this ticket's own defect.
                        "filters": {
                            "txn_type": "expense",
                            "date_range": last_12_months,
                        },
                    },
                },
            ],
        },
    },
    {
        "key": "account_balances",
        "name": "Account balances",
        "description": (
            "Where your money sits right now: total balance across all "
            "accounts, plus a breakdown by account and by account type."
        ),
        # Accounts source reports a current snapshot, not a period, so it
        # ignores the canvas date range — no window needed.
        "canvas_filters_json": {},
        "layout_json": {
            "version": 1,
            "widgets": [
                {
                    "id": "ab-kpi-total",
                    "type": "kpi",
                    "title": "Total balance",
                    "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "accounts",
                        "measure": _measure("sum", "balance"),
                    },
                },
                {
                    "id": "ab-bar-by-account",
                    "type": "bar",
                    "title": "Balance by account",
                    "grid": {"x": 0, "y": 2, "w": 6, "h": 4},
                    "config": {
                        "dataset": "accounts",
                        "measure": _measure("sum", "balance"),
                        "dimensions": ["account"],
                        "sort": {"by": "value", "dir": "desc"},
                        "limit": 10,
                    },
                },
                {
                    "id": "ab-bar-by-type",
                    "type": "bar",
                    "title": "Balance by account type",
                    "grid": {"x": 6, "y": 2, "w": 6, "h": 4},
                    "config": {
                        "dataset": "accounts",
                        "measure": _measure("sum", "balance"),
                        "dimensions": ["account_type"],
                        "sort": {"by": "value", "dir": "desc"},
                    },
                },
            ],
        },
    },
    {
        "key": "settled_vs_pending",
        "name": "Settled vs pending",
        "description": (
            "This month's activity split by settlement status, with net, "
            "so you can see how much is still in flight."
        ),
        "canvas_filters_json": {"date_range": this_month},
        "layout_json": {
            "version": 1,
            "widgets": [
                {
                    "id": "svp-kpi-net",
                    "type": "kpi",
                    "title": "Net (this month)",
                    "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                    },
                },
                {
                    "id": "svp-bar-status",
                    "type": "bar",
                    "title": "Amount by status",
                    "grid": {"x": 0, "y": 2, "w": 8, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measure": _measure("sum"),
                        "dimensions": ["status"],
                        "sort": {"by": "value", "dir": "desc"},
                    },
                },
            ],
        },
    },
]
