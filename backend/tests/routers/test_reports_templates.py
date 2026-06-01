"""Router tests for Reports v2 version history + "Revert to original".

create_report records one ``is_original=True`` ``report_versions`` row
from the create-time layout/filters. Save (PATCH) records a new
non-original version ONLY when ``layout_json`` / ``canvas_filters_json``
change, capped at 5 total (original pinned + 4 most-recent). Restore /
reset copy a version back into the live report without adding a version.

The version rows are read back from the DB via the test
``session_factory``.

Fixtures (``session_factory``, ``_make_app``, ``_seed``, ``_resolver``,
``_enable_flag``) are reused from ``test_reports.py``.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings as app_settings
from app.models.report import Report, ReportVersion, ReportVisibility

from tests.routers.test_reports import (  # noqa: F401 — reuse fixtures
    _enable_flag,
    _make_app,
    _resolver,
    _seed,
    session_factory,
)


_LAYOUT = {"version": 1, "widgets": [{"id": "w1", "type": "kpi"}]}
_FILTERS = {"date_range": {"kind": "relative", "preset": "last_30_days"}}


async def _versions_for(session_factory, report_id):
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .order_by(ReportVersion.created_at.asc(), ReportVersion.id.asc())
            )
        ).scalars().all()
        return list(rows)


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

    versions = await _versions_for(session_factory, report_id)
    assert len(versions) == 1
    original = versions[0]
    assert original.is_original is True
    assert original.layout_json == _LAYOUT
    assert original.canvas_filters_json == _FILTERS


@pytest.mark.asyncio
async def test_save_creates_version_and_caps_at_five(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Capped Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        # 6 distinct saves; only the 4 most recent non-originals survive.
        saved_layouts = []
        for i in range(6):
            layout = {"version": 1, "widgets": [{"id": f"w{i}", "type": "kpi"}]}
            saved_layouts.append(layout)
            patched = client.patch(f"/api/v1/reports/{report_id}", json={
                "layout_json": layout,
            })
            assert patched.status_code == 200, patched.text

    versions = await _versions_for(session_factory, report_id)
    assert len(versions) == 5
    originals = [v for v in versions if v.is_original]
    non_originals = [v for v in versions if not v.is_original]
    assert len(originals) == 1
    assert originals[0].layout_json == _LAYOUT  # creation layout pinned
    assert len(non_originals) == 4
    # The 4 most recent saves (saves 2..5, zero-indexed) survive in order.
    survived = [v.layout_json for v in non_originals]
    assert survived == saved_layouts[2:]


@pytest.mark.asyncio
async def test_patch_without_layout_change_does_not_version(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Meta Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        patched = client.patch(f"/api/v1/reports/{report_id}", json={
            "name": "Renamed",
            "visibility": "org",
        })
        assert patched.status_code == 200, patched.text

    versions = await _versions_for(session_factory, report_id)
    # Only the create-time original; the metadata-only PATCH added nothing.
    assert len(versions) == 1
    assert versions[0].is_original is True


@pytest.mark.asyncio
async def test_patch_identical_layout_does_not_version(session_factory):
    """Re-sending the current layout/filters verbatim must not snapshot.

    A no-op save would otherwise burn one of the 4 non-original slots and
    prematurely evict meaningful history under the bounded cap.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Idempotent Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        # PATCH with values identical to the live state.
        patched = client.patch(f"/api/v1/reports/{report_id}", json={
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert patched.status_code == 200, patched.text

    versions = await _versions_for(session_factory, report_id)
    # Still just the create-time original; the no-op save added nothing.
    assert len(versions) == 1
    assert versions[0].is_original is True


@pytest.mark.asyncio
async def test_patch_null_layout_rejected_with_422(session_factory):
    """Explicit null for the NOT-NULL JSON columns must 422, not 500."""
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Null Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        for field in ("layout_json", "canvas_filters_json"):
            patched = client.patch(
                f"/api/v1/reports/{report_id}", json={field: None}
            )
            assert patched.status_code == 422, patched.text

    # No version churn from the rejected writes; only the original remains.
    versions = await _versions_for(session_factory, report_id)
    assert len(versions) == 1
    assert versions[0].is_original is True


@pytest.mark.asyncio
async def test_list_versions(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "List Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        assert res.status_code == 201, res.text
        report_id = res.json()["id"]

        for i in range(2):
            patched = client.patch(f"/api/v1/reports/{report_id}", json={
                "layout_json": {"version": 1, "widgets": [{"id": f"s{i}"}]},
            })
            assert patched.status_code == 200, patched.text

        listed = client.get(f"/api/v1/reports/{report_id}/versions")
        assert listed.status_code == 200, listed.text
        body = listed.json()

    assert len(body) == 3
    # Newest-first ordering.
    ids = [v["id"] for v in body]
    assert ids == sorted(ids, reverse=True)
    # The original is the oldest entry (last in newest-first list).
    assert body[-1]["is_original"] is True
    assert [v["is_original"] for v in body[:-1]] == [False, False]
    # Summary shape only.
    assert set(body[0].keys()) == {"id", "is_original", "created_at"}


@pytest.mark.asyncio
async def test_restore_version_sets_live_and_does_not_add_version(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    layout_v2 = {"version": 1, "widgets": [{"id": "v2"}]}
    layout_v3 = {"version": 1, "widgets": [{"id": "v3"}]}
    with TestClient(app) as client:
        res = client.post("/api/v1/reports", json={
            "name": "Restore Report",
            "visibility": "private",
            "layout_json": _LAYOUT,
            "canvas_filters_json": _FILTERS,
        })
        report_id = res.json()["id"]

        client.patch(f"/api/v1/reports/{report_id}", json={"layout_json": layout_v2})
        client.patch(f"/api/v1/reports/{report_id}", json={"layout_json": layout_v3})

        listed = client.get(f"/api/v1/reports/{report_id}/versions").json()
        count_before = len(listed)
        # Pick the v2 (older non-original) entry to restore.
        target = None
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(ReportVersion).where(
                        ReportVersion.report_id == report_id,
                        ReportVersion.layout_json == layout_v2,
                    )
                )
            ).scalars().all()
            target = rows[0].id

        restored = client.post(
            f"/api/v1/reports/{report_id}/versions/{target}/restore"
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["layout_json"] == layout_v2

        listed_after = client.get(f"/api/v1/reports/{report_id}/versions").json()

    assert len(listed_after) == count_before  # restore added no version

    async with session_factory() as db:
        row = (
            await db.execute(select(Report).where(Report.id == report_id))
        ).scalar_one()
        assert row.layout_json == layout_v2


@pytest.mark.asyncio
async def test_restore_404_for_version_of_other_report(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        a = client.post("/api/v1/reports", json={
            "name": "A", "visibility": "private",
            "layout_json": _LAYOUT, "canvas_filters_json": _FILTERS,
        }).json()
        b = client.post("/api/v1/reports", json={
            "name": "B", "visibility": "private",
            "layout_json": _LAYOUT, "canvas_filters_json": _FILTERS,
        }).json()

        b_versions = client.get(f"/api/v1/reports/{b['id']}/versions").json()
        b_version_id = b_versions[0]["id"]

        # Try to restore B's version onto report A → 404 (wrong report).
        res = client.post(
            f"/api/v1/reports/{a['id']}/versions/{b_version_id}/restore"
        )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_restore_forbidden_for_non_editor(session_factory):
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="org-shared",
            layout_json=_LAYOUT,
            canvas_filters_json=_FILTERS,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id
        ver = ReportVersion(
            report_id=rid,
            is_original=True,
            layout_json=_LAYOUT,
            canvas_filters_json=_FILTERS,
        )
        db.add(ver)
        await db.commit()
        await db.refresh(ver)
        vid = ver.id

    app = _make_app(session_factory, _resolver("member_a"))
    with TestClient(app) as client:
        res = client.post(f"/api/v1/reports/{rid}/versions/{vid}/restore")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_list_versions_404_when_not_found(session_factory):
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports/999999/versions")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_versions_404_when_flag_off(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/reports/1/versions")
    assert res.status_code == 404


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

        # Reset back to the as-created (original) version.
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

    # Reset (a restore) does not itself add a version: original + 1 save.
    versions = await _versions_for(session_factory, report_id)
    assert len(versions) == 2
    assert sum(1 for v in versions if v.is_original) == 1


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


# ─── POST /api/v1/reports/{id}/duplicate ────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_creates_private_copy_for_caller(session_factory):
    seeds = await _seed(session_factory)
    # An ORG-shared report owned by user_a; user_a duplicates it.
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="Quarterly",
            layout_json=_LAYOUT,
            canvas_filters_json=_FILTERS,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post(f"/api/v1/reports/{rid}/duplicate")
        assert res.status_code == 201, res.text
        body = res.json()

    assert body["id"] != rid
    assert body["owner_user_id"] == seeds["user_a_id"]
    assert body["org_id"] == seeds["org_a_id"]
    assert body["visibility"] == "private"
    assert body["name"] == "Quarterly (copy)"
    assert body["layout_json"] == _LAYOUT
    assert body["canvas_filters_json"] == _FILTERS

    # The copy gets exactly one is_original version snapshot.
    versions = await _versions_for(session_factory, body["id"])
    assert len(versions) == 1
    assert versions[0].is_original is True
    assert versions[0].layout_json == _LAYOUT
    assert versions[0].canvas_filters_json == _FILTERS


@pytest.mark.asyncio
async def test_duplicate_by_org_member_owns_the_copy(session_factory):
    """A same-org member who can VIEW an org-shared report can duplicate
    it; the copy is private and owned by that member, not the original
    author.
    """
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.ORG,
            name="Shared",
            layout_json=_LAYOUT,
            canvas_filters_json=_FILTERS,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    app = _make_app(session_factory, _resolver("member_a"))
    with TestClient(app) as client:
        res = client.post(f"/api/v1/reports/{rid}/duplicate")
        assert res.status_code == 201, res.text
        body = res.json()

    assert body["owner_user_id"] == seeds["member_a_id"]
    assert body["visibility"] == "private"
    assert body["name"] == "Shared (copy)"


@pytest.mark.asyncio
async def test_duplicate_404_for_non_viewer(session_factory):
    """A user in a different org cannot see (and thus cannot duplicate)
    a private report — 404, never leaking existence.
    """
    seeds = await _seed(session_factory)
    async with session_factory() as db:
        report = Report(
            owner_user_id=seeds["user_a_id"],
            org_id=seeds["org_a_id"],
            visibility=ReportVisibility.PRIVATE,
            name="Private A",
            layout_json=_LAYOUT,
            canvas_filters_json=_FILTERS,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        rid = report.id

    # user_b is in org B.
    app = _make_app(session_factory, _resolver("user_b"))
    with TestClient(app) as client:
        res = client.post(f"/api/v1/reports/{rid}/duplicate")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_404_when_flag_off(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_reports_v2", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.post("/api/v1/reports/1/duplicate")
    assert res.status_code == 404
