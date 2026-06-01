"""Router tests for Reports v2 "Revert to original" snapshot columns.

create_report must populate ``original_layout_json`` +
``original_canvas_filters_json`` from the create-time layout/filters so a
future "Revert to original" can restore them. PATCH must NOT touch the
snapshot.

The snapshot columns are not exposed in the API response, so assertions
read the ``Report`` row back from the DB via the test ``session_factory``.

Fixtures (``session_factory``, ``_make_app``, ``_seed``, ``_resolver``,
``_enable_flag``) are reused from ``test_reports.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.report import Report

from tests.routers.test_reports import (  # noqa: F401 — reuse fixtures
    _enable_flag,
    _make_app,
    _resolver,
    _seed,
    session_factory,
)


_LAYOUT = {"version": 1, "widgets": [{"id": "w1", "type": "kpi"}]}
_FILTERS = {"date_range": {"kind": "relative", "preset": "last_30_days"}}


@pytest.mark.asyncio
async def test_create_snapshots_original_layout_and_filters(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Snapshot Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

    async with session_factory() as db:
        row = (
            await db.execute(select(Report).where(Report.id == report_id))
        ).scalar_one()
        assert row.original_layout_json == _LAYOUT
        assert row.original_canvas_filters_json == _FILTERS


@pytest.mark.asyncio
async def test_patch_does_not_touch_original_snapshot(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Snapshot Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        patched = client.patch(f"/api/v1/reports/{report_id}", json={
            "layout_json": {"version": 1, "widgets": []},
        })
        assert patched.status_code == 200, patched.text

    async with session_factory() as db:
        row = (
            await db.execute(select(Report).where(Report.id == report_id))
        ).scalar_one()
        # live layout changed, snapshot must be untouched
        assert row.layout_json == {"version": 1, "widgets": []}
        assert row.original_layout_json == _LAYOUT
        assert row.original_canvas_filters_json == _FILTERS
