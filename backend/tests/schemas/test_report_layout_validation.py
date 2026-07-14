"""Strict ``layout_json`` / ``canvas_filters_json`` validation (PR2).

Pins the write-side contract added by the report layout validator:

- Empty dicts (blank / new report) accepted.
- The three shipped starter templates validate (the canonical valid
  shapes the frontend actually sends).
- Single-measure vs multi-series config split is enforced.
- Malformed layouts (bad widget type / agg / dimension, missing grid,
  zero-size grid, duplicate ids, wrong version, extra envelope keys,
  measure on a multi-series widget, etc.) are rejected.
- Canvas filters: valid shape accepted, unknown key rejected.
"""
import copy

import pytest
from pydantic import ValidationError

from app.reports.templates import get_report_templates
from app.schemas.report import ReportCreate, ReportUpdate


# ─── valid ──────────────────────────────────────────────────────────


def test_empty_layout_and_filters_accepted():
    body = ReportCreate(name="blank")
    assert body.layout_json == {}
    assert body.canvas_filters_json == {}


def test_explicit_empty_dicts_accepted():
    body = ReportCreate(
        name="blank",
        layout_json={},
        canvas_filters_json={},
    )
    assert body.layout_json == {}
    assert body.canvas_filters_json == {}


def test_empty_widgets_list_accepted():
    body = ReportCreate(
        name="empty canvas",
        layout_json={"version": 1, "widgets": []},
    )
    assert body.layout_json["widgets"] == []


@pytest.mark.parametrize("template", get_report_templates())
def test_starter_templates_validate(template):
    """Every shipped starter template is a valid create payload."""
    body = ReportCreate(
        name=template["name"],
        layout_json=template["layout_json"],
        canvas_filters_json=template["canvas_filters_json"],
    )
    assert body.layout_json["version"] == 1
    assert len(body.layout_json["widgets"]) >= 1


def _kpi_layout():
    return {
        "version": 1,
        "widgets": [
            {
                "id": "k1",
                "type": "kpi",
                "title": "Net",
                "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                "config": {
                    "dataset": "transactions",
                    "measure": {"agg": "sum", "field": "amount"},
                    "format": "currency",
                },
            }
        ],
    }


def _line_layout():
    return {
        "version": 1,
        "widgets": [
            {
                "id": "l1",
                "type": "line",
                "title": "Net trend",
                "grid": {"x": 0, "y": 0, "w": 6, "h": 4},
                "config": {
                    "dataset": "transactions",
                    "measures": [
                        {"measure": {"agg": "sum", "field": "amount"}, "label": "Net"}
                    ],
                    "dimensions": ["day"],
                },
            }
        ],
    }


def _sankey_layout():
    return {
        "version": 1,
        "widgets": [
            {
                "id": "s1",
                "type": "sankey",
                "title": "Cash flow",
                "grid": {"x": 0, "y": 0, "w": 8, "h": 5},
                "config": {
                    "dataset": "transactions",
                    "measure": {"agg": "sum", "field": "amount"},
                    "spending_granularity": "category",
                },
            }
        ],
    }


def test_single_measure_kpi_valid():
    body = ReportCreate(name="r", layout_json=_kpi_layout())
    assert body.layout_json["widgets"][0]["type"] == "kpi"


def test_sankey_widget_valid():
    """A sankey widget must validate — regression guard for the missing
    WidgetType.SANKEY enum member that 422'd every Cash-flow report save."""
    body = ReportCreate(name="r", layout_json=_sankey_layout())
    assert body.layout_json["widgets"][0]["type"] == "sankey"


def test_sankey_widget_round_trips_granularity_and_top_n_verbatim():
    """spending_granularity + top_n must survive validation verbatim."""
    layout = _sankey_layout()
    layout["widgets"][0]["config"]["spending_granularity"] = "category_master"
    layout["widgets"][0]["config"]["top_n"] = 8
    body = ReportCreate(name="r", layout_json=layout)
    cfg = body.layout_json["widgets"][0]["config"]
    assert cfg["spending_granularity"] == "category_master"
    assert cfg["top_n"] == 8


def test_sankey_widget_rejects_missing_measure():
    """measure is required on the sankey config (mirrors the other widgets'
    required-field contract)."""
    layout = _sankey_layout()
    layout["widgets"][0]["config"].pop("measure")
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_sankey_widget_rejects_bad_spending_granularity():
    """spending_granularity is a closed enum (category | category_master)."""
    layout = _sankey_layout()
    layout["widgets"][0]["config"]["spending_granularity"] = "daily"
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_sankey_widget_rejects_non_positive_top_n():
    """top_n must be >= 1 (ge=1) — a zero/negative cap is malformed."""
    layout = _sankey_layout()
    layout["widgets"][0]["config"]["top_n"] = 0
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_multi_series_line_valid():
    body = ReportCreate(name="r", layout_json=_line_layout())
    assert body.layout_json["widgets"][0]["type"] == "line"


def test_unknown_config_extra_is_tolerated():
    """Forward-compat: an unmodelled optional visual knob does not 422 AND
    survives validation (must NOT be silently stripped — these are real,
    widget-consumed knobs like smooth/top_n/stacked/compare_prior_period)."""
    layout = _line_layout()
    layout["widgets"][0]["config"]["smooth"] = True
    layout["widgets"][0]["config"]["some_future_knob"] = {"a": 1}
    body = ReportCreate(name="r", layout_json=layout)
    cfg = body.layout_json["widgets"][0]["config"]
    assert body.layout_json["widgets"][0]["type"] == "line"
    # The validator must validate for side-effect only and preserve the
    # original dict verbatim — not round-trip through model_dump.
    assert cfg["smooth"] is True
    assert cfg["some_future_knob"] == {"a": 1}


def test_valid_canvas_filters():
    body = ReportCreate(
        name="r",
        canvas_filters_json={
            "date_range": {"start": "2026-01-01", "end": "2026-01-31"},
            "account_ids": [1, 2],
            "category_ids": [3],
        },
    )
    assert body.canvas_filters_json["account_ids"] == [1, 2]


def test_valid_canvas_filters_status():
    # Canvas-level status (the #538-era widget-only filter, now cascaded)
    # persists in canvas_filters_json.
    body = ReportCreate(name="r", canvas_filters_json={"status": "settled"})
    assert body.canvas_filters_json["status"] == "settled"


def test_valid_canvas_filters_next_cycle_preset():
    # A relative date token persists as the canvas date_range preset.
    body = ReportCreate(
        name="r",
        canvas_filters_json={"date_range": {"preset": "next_cycle"}},
    )
    assert body.canvas_filters_json["date_range"]["preset"] == "next_cycle"


# ─── malformed → ValidationError ────────────────────────────────────


def _mutate(fn):
    layout = _kpi_layout()
    fn(layout)
    return layout


def test_reject_unknown_widget_type():
    layout = _mutate(lambda l: l["widgets"][0].__setitem__("type", "gauge"))
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


@pytest.mark.parametrize(
    "dash_type",
    [
        "dash_on_track",
        "dash_accounts",
        "dash_account_forecast",
        "dash_spending",
        "dash_budget",
        "dash_forecast_category",
        "dash_recent_transactions",
    ],
)
def test_reject_dashboard_native_types_no_smuggling(dash_type):
    """The strict reports validator must REJECT dashboard-native dash_* types.

    Dashboard tiles are accepted only by the dashboard validator
    (validate_dashboard_layout_json). A report layout_json carrying a dash_*
    widget must 422 — the two surfaces use separate tables and there is no
    cross-surface smuggling.
    """
    layout = _mutate(lambda l: l["widgets"][0].__setitem__("type", dash_type))
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_bad_agg():
    layout = _kpi_layout()
    layout["widgets"][0]["config"]["measure"]["agg"] = "median"
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_bad_dimension():
    layout = _line_layout()
    layout["widgets"][0]["config"]["dimensions"] = ["galaxy"]
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_missing_grid():
    layout = _mutate(lambda l: l["widgets"][0].pop("grid"))
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_zero_size_grid():
    layout = _kpi_layout()
    layout["widgets"][0]["grid"]["w"] = 0
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_missing_id():
    layout = _mutate(lambda l: l["widgets"][0].pop("id"))
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_empty_id():
    layout = _kpi_layout()
    layout["widgets"][0]["id"] = ""
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_duplicate_widget_ids():
    layout = _kpi_layout()
    layout["widgets"].append(copy.deepcopy(layout["widgets"][0]))
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_wrong_version():
    layout = _kpi_layout()
    layout["version"] = 2
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_missing_version():
    layout = _kpi_layout()
    layout.pop("version")
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_extra_envelope_key():
    layout = _kpi_layout()
    layout["unexpected"] = True
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_extra_widget_envelope_key():
    layout = _kpi_layout()
    layout["widgets"][0]["surprise"] = 1
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_single_measure_widget_with_measures():
    """A kpi must carry ``measure``, not the multi-series ``measures``."""
    layout = _kpi_layout()
    layout["widgets"][0]["config"].pop("measure")
    layout["widgets"][0]["config"]["measures"] = [
        {"measure": {"agg": "sum", "field": "amount"}}
    ]
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_multi_series_widget_with_measure():
    """A line must carry ``measures``, not the single ``measure``."""
    layout = _line_layout()
    layout["widgets"][0]["config"].pop("measures")
    layout["widgets"][0]["config"]["measure"] = {"agg": "sum", "field": "amount"}
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_empty_measures_list():
    layout = _line_layout()
    layout["widgets"][0]["config"]["measures"] = []
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_bad_measure_field():
    layout = _kpi_layout()
    layout["widgets"][0]["config"]["measure"]["field"] = "secret_column"
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json=layout)


def test_reject_widgets_not_a_list():
    with pytest.raises(ValidationError):
        ReportCreate(name="r", layout_json={"version": 1, "widgets": {}})


def test_reject_canvas_filters_unknown_key():
    with pytest.raises(ValidationError):
        ReportCreate(name="r", canvas_filters_json={"surprise": 1})


def test_reject_canvas_filters_bad_account_ids():
    with pytest.raises(ValidationError):
        ReportCreate(
            name="r", canvas_filters_json={"account_ids": ["not-an-int"]}
        )


def test_reject_canvas_filters_bad_status():
    with pytest.raises(ValidationError):
        ReportCreate(name="r", canvas_filters_json={"status": "cleared"})


def test_reject_canvas_filters_bad_preset():
    with pytest.raises(ValidationError):
        ReportCreate(
            name="r", canvas_filters_json={"date_range": {"preset": "yesteryear"}}
        )


def test_reject_canvas_filters_preset_with_absolute_dates():
    # A relative preset carries no absolute window; a blob with both is
    # contradictory and rejected (the _preset_excludes_absolute invariant).
    with pytest.raises(ValidationError):
        ReportCreate(
            name="r",
            canvas_filters_json={
                "date_range": {"preset": "next_cycle", "start": "2026-01-01"}
            },
        )


# ─── update path mirrors create ─────────────────────────────────────


def test_update_validates_layout():
    with pytest.raises(ValidationError):
        ReportUpdate(layout_json={"version": 1, "widgets": [{"type": "nope"}]})


def test_update_none_layout_passes_through():
    """An absent layout (None) is left for the router's null guard."""
    body = ReportUpdate(name="rename only")
    assert body.layout_json is None


def test_update_accepts_valid_layout():
    body = ReportUpdate(layout_json=_kpi_layout())
    assert body.layout_json["widgets"][0]["type"] == "kpi"
