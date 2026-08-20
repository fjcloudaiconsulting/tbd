"""TBD-382 F12 — the ``cdd-stacked-by-month`` starter template.

The template ships ``dimensions: ["month", "category"]`` with a SINGLE
measure. Before TBD-382 the frontend read ``dimensions[0]`` only and merged
rows last-write-wins on the month label, so every category in a month
collapsed onto the month and only the last survived -- rendered as if it
were the month's total. The compiler applies a default ``ORDER BY value
DESC`` when the AST carries no ``sort``, and this template carried none, so
each month's bar showed that month's SMALLEST category, labelled as the
month.

R9 gives it an explicit ascending dimension sort and an explicit limit. Both
ship as bugfix: the sort changes no data and there is no state in which
"months in spend order" is the intended reading of a time axis, and the limit
is a no-op on any window holding <= 12 months.

⚠ The one-measure assertion is load-bearing, not decorative.
``_MultiSeriesConfig.measures`` carries ``Field(min_length=1)`` and NO max, so
a future template edit adding a second measure would validate cleanly while
asserting that two measures sum to something meaningful -- which is false on
every published measure pair on every source (R1).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.reports.templates import _this_month_range, get_report_templates
from app.schemas.report_layout import validate_layout_json

TEMPLATE_KEY = "category_deep_dive"
WIDGET_ID = "cdd-stacked-by-month"


def _template(key: str) -> dict:
    for tpl in get_report_templates():
        if tpl["key"] == key:
            return tpl
    raise AssertionError(f"template {key!r} not found")


def _widget(key: str, widget_id: str) -> dict:
    for widget in _template(key)["layout_json"]["widgets"]:
        if widget["id"] == widget_id:
            return widget
    raise AssertionError(f"widget {widget_id!r} not found in template {key!r}")


def test_stacked_by_month_sorts_its_time_axis_chronologically() -> None:
    widget = _widget(TEMPLATE_KEY, WIDGET_ID)
    assert widget["type"] == "stacked_bar"
    assert widget["config"]["dimensions"] == ["month", "category"]
    assert widget["config"]["sort"] == {"by": "dimension", "dir": "asc"}


def test_stacked_by_month_carries_an_explicit_limit() -> None:
    limit = _widget(TEMPLATE_KEY, WIDGET_ID)["config"].get("limit")
    assert limit is not None, (
        "an absent limit leaves the client-side primary cap on its default; "
        "the template must state the number of month buckets it wants"
    )
    assert isinstance(limit, int) and limit >= 12


def test_stacked_by_month_carries_exactly_one_measure() -> None:
    measures = _widget(TEMPLATE_KEY, WIDGET_ID)["config"]["measures"]
    assert len(measures) == 1, (
        "stacked_bar stacks by dimensions[1], never by measures: no pair of "
        "published measures on any source has a meaningful sum"
    )


def test_every_starter_template_still_validates_against_layout_json() -> None:
    for tpl in get_report_templates():
        validate_layout_json(tpl["layout_json"])


@pytest.mark.parametrize("tpl", get_report_templates(), ids=lambda t: t["key"])
def test_no_starter_stacked_bar_carries_more_than_one_measure(tpl: dict) -> None:
    for widget in tpl["layout_json"]["widgets"]:
        if widget["type"] == "stacked_bar":
            assert len(widget["config"]["measures"]) == 1


def test_stacked_by_month_carries_its_own_multi_month_window() -> None:
    """The panel declares its OWN window instead of inheriting the canvas.

    Grouping by month over the canvas's one-month window is structurally a
    ONE-BAR chart, so this panel -- the canvas's only time-series view --
    carries a trailing-12-month ``date_range`` of its own.
    """
    config = _widget(TEMPLATE_KEY, WIDGET_ID)["config"]
    date_range = config.get("filters", {}).get("date_range")
    assert date_range is not None, (
        "grouping by month over the canvas's one-month window renders a "
        "single bar; the panel must declare its own window"
    )
    start = date.fromisoformat(date_range["start"])
    end = date.fromisoformat(date_range["end"])
    span_months = (end.year - start.year) * 12 + (end.month - start.month)
    assert span_months > 1, (
        f"window {date_range} spans {span_months} month(s); a month-grouped "
        "chart needs more than one bucket to stack anything"
    )


def test_category_deep_dive_canvas_window_is_untouched() -> None:
    """⚠ Kills the "just widen the canvas" implementation.

    Widening ``canvas_filters_json`` silently redefines "Category share" and
    "Top categories" as twelve-month questions and puts an INCOME row at the
    top of a spend canvas. The override is per-widget, and the canvas stays
    on the current month.
    """
    today = date.today()
    assert _template(TEMPLATE_KEY)["canvas_filters_json"] == {
        "date_range": _this_month_range(today)
    }
