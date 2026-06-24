"""Dashboard schema tests — DashboardLayoutOut + DashboardUpdate.

Coverage:
- DashboardUpdate accepts empty / None fields (partial update).
- layout_json and canvas_filters_json round-trip VERBATIM (guards #424
  regression where model_dump-round-trip strips unmodelled visual knobs).
- Validation rejects malformed layout_json / canvas_filters_json.
- extra="forbid" on DashboardUpdate rejects unknown keys.
- DashboardLayoutOut round-trips from ORM-like dicts (from_attributes).
"""
from __future__ import annotations

import copy
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.dashboard import DashboardLayoutOut, DashboardUpdate


# ─── DashboardUpdate — basic field acceptance ───────────────────────


def test_dashboard_update_all_none():
    """Both fields absent (None) — valid partial update."""
    upd = DashboardUpdate()
    assert upd.layout_json is None
    assert upd.canvas_filters_json is None


def test_dashboard_update_empty_dicts_accepted():
    """Explicit empty dicts are valid (blank / new dashboard)."""
    upd = DashboardUpdate(layout_json={}, canvas_filters_json={})
    assert upd.layout_json == {}
    assert upd.canvas_filters_json == {}


def test_dashboard_update_forbids_extra_keys():
    """extra="forbid" must reject unknown keys."""
    with pytest.raises(ValidationError):
        DashboardUpdate(unknown_key="bad")


# ─── verbatim round-trip guard (anti-#424 regression) ──────────────


# A realistic layout_json with a KPI widget plus an unmodelled visual knob
# (``compare_prior_period``) that would be stripped by a model_dump round-trip
# because the widget config uses ``extra="ignore"``.
_KPI_WITH_KNOB = {
    "version": 1,
    "widgets": [
        {
            "id": "w1",
            "type": "kpi",
            "title": "Total spend",
            "grid": {"x": 0, "y": 0, "w": 2, "h": 2},
            "config": {
                "dataset": "transactions",
                "measure": {"agg": "sum", "field": "amount"},
                # Unmodelled visual knob — must survive the round-trip:
                "compare_prior_period": True,
                "top_n": 10,
            },
        }
    ],
}

_MULTI_SERIES_WITH_KNOB = {
    "version": 1,
    "widgets": [
        {
            "id": "w2",
            "type": "line",
            "title": "Trend",
            "grid": {"x": 0, "y": 0, "w": 4, "h": 3},
            "config": {
                "dataset": "transactions",
                "measures": [
                    {"measure": {"agg": "sum", "field": "amount"}, "label": "Spend"}
                ],
                "dimensions": ["month"],
                # Unmodelled knobs:
                "smooth": True,
                "stacked": False,
            },
        }
    ],
}


def test_layout_json_round_trips_verbatim_single_measure():
    """layout_json with unmodelled knobs must be returned byte-for-byte (no strip)."""
    payload = copy.deepcopy(_KPI_WITH_KNOB)
    upd = DashboardUpdate(layout_json=payload)
    # The returned dict must be identical to the input, knobs intact.
    assert upd.layout_json == _KPI_WITH_KNOB
    assert upd.layout_json["widgets"][0]["config"]["compare_prior_period"] is True
    assert upd.layout_json["widgets"][0]["config"]["top_n"] == 10


def test_layout_json_round_trips_verbatim_multi_series():
    """Multi-series layout with unmodelled knobs also round-trips verbatim."""
    payload = copy.deepcopy(_MULTI_SERIES_WITH_KNOB)
    upd = DashboardUpdate(layout_json=payload)
    assert upd.layout_json == _MULTI_SERIES_WITH_KNOB
    assert upd.layout_json["widgets"][0]["config"]["smooth"] is True
    assert upd.layout_json["widgets"][0]["config"]["stacked"] is False


def test_canvas_filters_json_round_trips_verbatim():
    """canvas_filters_json round-trips verbatim."""
    filters = {"date_range": {"start": "2026-01-01", "end": "2026-06-30"}, "account_ids": [1, 2]}
    upd = DashboardUpdate(canvas_filters_json=filters)
    assert upd.canvas_filters_json == filters


# ─── validation rejects bad layout ─────────────────────────────────


def test_dashboard_update_rejects_bad_widget_type():
    """A layout with an unknown widget type must be rejected (422-equivalent)."""
    bad_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w1",
                "type": "unknown_type",
                "title": "X",
                "grid": {"x": 0, "y": 0, "w": 2, "h": 2},
                "config": {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}},
            }
        ],
    }
    with pytest.raises(ValidationError):
        DashboardUpdate(layout_json=bad_layout)


def test_dashboard_update_rejects_duplicate_widget_ids():
    """Duplicate widget ids must be rejected."""
    bad_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "dup",
                "type": "kpi",
                "title": "A",
                "grid": {"x": 0, "y": 0, "w": 2, "h": 2},
                "config": {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}},
            },
            {
                "id": "dup",
                "type": "kpi",
                "title": "B",
                "grid": {"x": 2, "y": 0, "w": 2, "h": 2},
                "config": {"dataset": "transactions", "measure": {"agg": "sum", "field": "amount"}},
            },
        ],
    }
    with pytest.raises(ValidationError):
        DashboardUpdate(layout_json=bad_layout)


def test_dashboard_update_rejects_bad_canvas_filters():
    """canvas_filters_json with an unknown key must be rejected."""
    with pytest.raises(ValidationError):
        DashboardUpdate(canvas_filters_json={"totally_unknown_key": True})


# ─── DashboardLayoutOut — from_attributes round-trip ───────────────


def test_dashboard_layout_out_from_dict():
    """DashboardLayoutOut.model_validate works from an ORM-like dict."""
    now = datetime(2026, 6, 24, 12, 0, 0)
    data = {
        "id": 1,
        "owner_user_id": 42,
        "org_id": 7,
        "layout_json": _KPI_WITH_KNOB,
        "canvas_filters_json": {},
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
    }
    out = DashboardLayoutOut.model_validate(data)
    assert out.id == 1
    assert out.owner_user_id == 42
    assert out.org_id == 7
    assert out.layout_json == _KPI_WITH_KNOB
    assert out.schema_version == 1
    assert out.created_at == now
