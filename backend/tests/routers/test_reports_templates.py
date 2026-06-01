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

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings as app_settings
from app.models.report import Report, ReportVisibility

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


# ─── GET /api/v1/reports/templates ──────────────────────────────────


@pytest.mark.asyncio
async def test_templates_endpoint_returns_three(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports/templates")
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body) == 3
    keys = {t["key"] for t in body}
    assert keys == {"monthly_review", "cash_flow_trend", "category_deep_dive"}
    for t in body:
        assert t["layout_json"]["widgets"], t["key"]


@pytest.mark.asyncio
async def test_templates_date_windows_reflect_current_month(session_factory):
    # The date windows must be computed per request from date.today(),
    # not frozen at module import. Assert the monthly_review window starts
    # on the first day of the current month and ends on the last day.
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports/templates")
    assert res.status_code == 200, res.text
    body = res.json()

    today = date.today()
    first_of_month = today.replace(day=1)

    by_key = {t["key"]: t for t in body}
    monthly = by_key["monthly_review"]
    date_range = monthly["canvas_filters_json"]["date_range"]
    assert date_range["start"] == first_of_month.isoformat()
    # end is the last day of the current month (>= today, same month)
    end = date.fromisoformat(date_range["end"])
    assert end >= today
    assert end.year == today.year and end.month == today.month

    # trailing-12-months template ends at today
    cash_flow = by_key["cash_flow_trend"]
    cf_range = cash_flow["canvas_filters_json"]["date_range"]
    assert cf_range["end"] == today.isoformat()
    assert cf_range["start"] == date(today.year - 1, today.month, 1).isoformat()


@pytest.mark.asyncio
async def test_templates_endpoint_404_when_flag_off(session_factory, monkeypatch):
    # The autouse ``_enable_flag`` fixture force-enables the flag; flip
    # it back off here so the router-level dep fires its 404.
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports/templates")
    assert res.status_code == 404


# ─── POST /api/v1/reports/{id}/reset ────────────────────────────────


_LAYOUT_A = {"version": 1, "widgets": [{"id": "w1", "type": "kpi"}]}
_FILTERS_X = {"date_range": {"kind": "relative", "preset": "last_30_days"}}
_LAYOUT_B = {"version": 1, "widgets": []}
_FILTERS_Y = {"date_range": {"kind": "relative", "preset": "last_7_days"}}


@pytest.mark.asyncio
async def test_reset_restores_original(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Reset Report",
            "visibility": "private",
            "layout_json": _LAYOUT_A,
            "canvas_filters_json": _FILTERS_X,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        # Edit the live state away from the original snapshot.
        patched = client.patch(f"/api/v1/reports/{report_id}", json={
            "layout_json": _LAYOUT_B,
            "canvas_filters_json": _FILTERS_Y,
        })
        assert patched.status_code == 200, patched.text
        assert patched.json()["layout_json"] == _LAYOUT_B
        assert patched.json()["canvas_filters_json"] == _FILTERS_Y

        # Reset back to the as-created snapshot.
        reset = client.post(f"/api/v1/reports/{report_id}/reset")
        assert reset.status_code == 200, reset.text
        body = reset.json()
        assert body["layout_json"] == _LAYOUT_A
        assert body["canvas_filters_json"] == _FILTERS_X

    # DB re-read confirms the live columns now match the original.
    async with session_factory() as db:
        row = (
            await db.execute(select(Report).where(Report.id == report_id))
        ).scalar_one()
        assert row.layout_json == _LAYOUT_A
        assert row.canvas_filters_json == _FILTERS_X
        # snapshot untouched
        assert row.original_layout_json == _LAYOUT_A
        assert row.original_canvas_filters_json == _FILTERS_X


@pytest.mark.asyncio
async def test_reset_forbidden_for_non_editor(session_factory):
    """A same-org MEMBER who is not the owner can VIEW an org-shared
    report but cannot reset it (mirrors the PATCH/DELETE edit gate).
    """
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],  # OWNER role authored it
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="org-shared",
            layout_json=_LAYOUT_B,
            canvas_filters_json=_FILTERS_Y,
            original_layout_json=_LAYOUT_A,
            original_canvas_filters_json=_FILTERS_X,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    # member_a is MEMBER role — can view but not edit.
    app = _make_app(session_factory, _resolver("member_a"))
    with TestClient(app) as client:
        res = client.post(f"/api/v1/reports/{rid}/reset")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_reset_404_when_not_found(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/999999/reset")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reset_404_when_flag_off(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/1/reset")
    assert res.status_code == 404
