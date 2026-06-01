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


_TODAY = date.today()
_THIS_MONTH = _this_month_range(_TODAY)
_LAST_12_MONTHS = _last_12_months_range(_TODAY)


REPORT_TEMPLATES: list[dict] = [
    {
        "key": "monthly_review",
        "name": "Monthly review",
        "description": (
            "Net, income, and expense at a glance for the current month, "
            "plus spend by category and a daily income-vs-expense trend."
        ),
        "canvas_filters_json": {"date_range": _THIS_MONTH},
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
                        "format": "currency",
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
                        "format": "currency",
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
                        "format": "currency",
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
                        "format": "currency",
                    },
                },
                {
                    "id": "mr-line-income-expense",
                    "type": "line",
                    "title": "Income vs expense",
                    "grid": {"x": 6, "y": 2, "w": 6, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Net")],
                        "dimensions": ["day"],
                        "format": "currency",
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
        "canvas_filters_json": {"date_range": _LAST_12_MONTHS},
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
                        "format": "currency",
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
                        "format": "currency",
                    },
                },
            ],
        },
    },
    {
        "key": "category_deep_dive",
        "name": "Category deep-dive",
        "description": (
            "Category share of spend, the top transactions table, and a "
            "stacked category-by-month breakdown."
        ),
        "canvas_filters_json": {"date_range": _THIS_MONTH},
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
                    "title": "Top transactions",
                    "grid": {"x": 6, "y": 0, "w": 6, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Amount")],
                        "dimensions": ["category"],
                        "sort": {"by": "value", "dir": "desc"},
                        "limit": 20,
                        "format": "currency",
                    },
                },
                {
                    "id": "cdd-stacked-by-month",
                    "type": "stacked_bar",
                    "title": "Category by month",
                    "grid": {"x": 0, "y": 4, "w": 12, "h": 4},
                    "config": {
                        "dataset": "transactions",
                        "measures": [_series("sum", "Spend")],
                        "dimensions": ["month", "category"],
                        "filters": {"txn_type": "expense"},
                        "format": "currency",
                    },
                },
            ],
        },
    },
]
