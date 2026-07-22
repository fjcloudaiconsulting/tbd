"""Router tests for GET/PATCH /api/v1/dashboard (W4 Phase 1).

Covers the six required cases:

(a) GET auto-creates a default layout when none exists and returns it.
(b) GET returns the same row on a second call (no duplicate created).
(c) PATCH owner updates layout_json, persists, and the response
    round-trips the full knob-bearing layout VERBATIM (validate-and-
    return-verbatim contract; no silent key stripping).
(d) A second user's GET is isolated — user B never sees user A's layout
    and gets their own default row.
(e) Feature gate OFF → 404 (require_feature raises 404 for gated routes).
(f) Unknown body key in PATCH → 422 (extra="forbid" on DashboardUpdate).
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.config import settings as app_settings
from app.database import get_db
from app.deps import get_current_user, get_session_factory
from app.models import Base
from app.models.user import Organization, Role, User
from app.routers.dashboard import router as dashboard_router
from app.security import hash_password


# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(Engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_app(session_factory, user_resolver):
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def override_user() -> User:
        return await user_resolver(session_factory)

    def override_factory():
        return session_factory

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_session_factory] = override_factory
    app.include_router(dashboard_router)
    return app


async def _seed(factory) -> dict:
    """Two orgs (A + B), one owner user each."""
    async with factory() as db:
        org_a = Organization(name="Org A", billing_cycle_day=1)
        org_b = Organization(name="Org B", billing_cycle_day=1)
        db.add_all([org_a, org_b])
        await db.commit()

        user_a = User(
            org_id=org_a.id,
            username="user_a",
            email="a@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        user_b = User(
            org_id=org_b.id,
            username="user_b",
            email="b@example.com",
            password_hash=hash_password("pw-1234567"),
            role=Role.OWNER,
            email_verified=True,
        )
        db.add_all([user_a, user_b])
        await db.commit()

        return {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
        }


def _resolver(username: str):
    async def resolve(session_factory):
        async with session_factory() as db:
            from sqlalchemy import select as _s
            return (
                await db.execute(_s(User).where(User.username == username))
            ).scalar_one()
    return resolve


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """Default every test in this file to feature_custom_dashboard ON.

    The gate-OFF test flips it back to False explicitly.
    """
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", True)


# ─── (a) GET auto-creates default layout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_auto_creates_default_layout(session_factory):
    """First GET for a user who has no row yet auto-creates a default and
    returns it with 200. The returned body includes id, owner_user_id,
    org_id, layout_json, canvas_filters_json, schema_version.
    """
    seeds = await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["owner_user_id"] == seeds["user_a_id"]
    assert body["org_id"] == seeds["org_a_id"]
    assert "layout_json" in body
    assert "canvas_filters_json" in body
    assert body["schema_version"] == 1
    assert body["id"] is not None


# ─── (b) Second GET returns the same row (no duplicate) ───────────────────────


@pytest.mark.asyncio
async def test_get_returns_same_row_on_second_call(session_factory):
    """Two successive GETs return the same dashboard row (same id, no
    duplicate insert violating the UNIQUE owner constraint).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res1 = client.get("/api/v1/dashboard")
        res2 = client.get("/api/v1/dashboard")
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["id"] == res2.json()["id"]


# ─── (c) PATCH updates + persists + round-trips verbatim ──────────────────────


@pytest.mark.asyncio
async def test_patch_updates_layout_round_trips_verbatim(session_factory):
    """PATCH with a knob-bearing layout_json persists to DB and the response
    round-trips it VERBATIM (no silent key stripping — validate-and-return-
    verbatim pattern, same as #424 fix in reports).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    knob_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w1",
                "type": "kpi",
                "title": "Total Spent",
                "grid": {"x": 0, "y": 0, "w": 2, "h": 2},
                "config": {
                    "dataset": "transactions",
                    "measure": {"agg": "sum", "field": "amount"},
                    # visual knobs that must NOT be silently stripped
                    "compare_prior_period": True,
                    "top_n": 10,
                },
            }
        ],
    }

    with TestClient(app) as client:
        # ensure row exists first
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard",
            json={"layout_json": knob_layout},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["layout_json"] == knob_layout

    # Verify persistence: a fresh GET returns the updated layout
    app2 = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app2) as client2:
        res2 = client2.get("/api/v1/dashboard")
    assert res2.status_code == 200
    assert res2.json()["layout_json"] == knob_layout


# ─── (d) Per-user scoping — user B never sees user A's layout ─────────────────


@pytest.mark.asyncio
async def test_get_is_isolated_per_user(session_factory):
    """User A and User B each get their own dashboard row.  User B's GET
    must NOT return user A's layout (different owner_user_id rows).
    """
    seeds = await _seed(session_factory)

    # User A creates + customises their layout first
    app_a = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app_a) as client_a:
        client_a.get("/api/v1/dashboard")
        client_a.patch(
            "/api/v1/dashboard",
            json={"layout_json": {"version": 1, "widgets": [{"id": "only-for-a"}]}},
        )

    # User B's GET should return their own (auto-created) row, not A's
    app_b = _make_app(session_factory, _resolver("user_b"))
    with TestClient(app_b) as client_b:
        res_b = client_b.get("/api/v1/dashboard")
    assert res_b.status_code == 200
    body_b = res_b.json()
    assert body_b["owner_user_id"] == seeds["user_b_id"]
    # org_id must be sourced from current_user, not bled across orgs
    assert body_b["org_id"] == seeds["org_b_id"]
    # Must NOT contain A's widget
    widgets = body_b["layout_json"].get("widgets", [])
    widget_ids = [w.get("id") for w in widgets]
    assert "only-for-a" not in widget_ids


# ─── (e) Feature gate OFF → 404 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_gate_off_returns_404_on_get(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_feature_gate_off_returns_404_on_patch(session_factory, monkeypatch):
    monkeypatch.setattr(app_settings, "feature_custom_dashboard", False)
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.patch("/api/v1/dashboard", json={"layout_json": {}})
    assert res.status_code == 404


# ─── (f) Unknown body key → 422 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_unknown_key_returns_422(session_factory):
    """DashboardUpdate uses extra='forbid'; an unknown key must be rejected
    with 422 Unprocessable Entity.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        # ensure row exists
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard",
            json={"layout_json": {}, "unknown_key": "bad"},
        )
    assert res.status_code == 422


# ─── (g) Explicit-null PATCH → 422 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_layout_json_null_returns_422(session_factory):
    """PATCH with explicit null for layout_json must be rejected with 422.

    The router's explicit-null guard (~line 141-147) blocks null values for
    NOT-NULL columns even though the Pydantic schema marks them Optional
    (so the field is patchable when absent but not when explicitly nulled).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": None})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_patch_canvas_filters_json_null_returns_422(session_factory):
    """PATCH with explicit null for canvas_filters_json must be rejected with 422.

    Mirrors test_patch_layout_json_null_returns_422 for the other guarded column.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard", json={"canvas_filters_json": None}
        )
    assert res.status_code == 422


# ─── (h) Default layout shape — 6 dash_* tiles (Phase 2a + 2b) ──────────────


@pytest.mark.asyncio
async def test_default_layout_contains_seven_dash_tiles(session_factory):
    """GET auto-creates a layout with all 7 Phase-2a+2b+2c finance tiles.

    The default layout must contain the Phase-2a tiles (dash_on_track,
    dash_accounts, dash_account_forecast) at their row-2 grid coords, the
    Phase-2b chart tiles (dash_spending, dash_budget, dash_forecast_category)
    at the row-3 grid coords, AND the Phase-2c recent-transactions tile
    (dash_recent_transactions) at the row-4 full-width coords.  The GET must
    return 200 (i.e. the dashboard layout validator accepts all 7 dash_* types).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard")
    assert res.status_code == 200, res.text
    body = res.json()
    widgets = body["layout_json"]["widgets"]
    types = [w["type"] for w in widgets]

    # Phase-2a tiles
    assert "dash_on_track" in types
    assert "dash_accounts" in types
    assert "dash_account_forecast" in types

    # Phase-2b chart tiles
    assert "dash_spending" in types
    assert "dash_budget" in types
    assert "dash_forecast_category" in types

    # Phase-2c recent-transactions tile
    assert "dash_recent_transactions" in types

    assert len(types) == 7

    # Verify grid coords match emptyDashboardWidget defaults. Heights are
    # sized so each tile fully shows its default content WITHOUT the card's
    # overflow-hidden clipping it (Reset-to-default sizing contract).
    by_type = {w["type"]: w for w in widgets}
    # Row 1
    assert by_type["dash_on_track"]["grid"] == {"x": 0, "y": 0, "w": 12, "h": 4}
    # Row 2
    assert by_type["dash_accounts"]["grid"] == {"x": 0, "y": 4, "w": 4, "h": 9}
    assert by_type["dash_account_forecast"]["grid"] == {"x": 4, "y": 4, "w": 8, "h": 9}
    # Row 3
    assert by_type["dash_spending"]["grid"] == {"x": 0, "y": 13, "w": 4, "h": 6}
    assert by_type["dash_budget"]["grid"] == {"x": 4, "y": 13, "w": 4, "h": 6}
    assert by_type["dash_forecast_category"]["grid"] == {"x": 8, "y": 13, "w": 4, "h": 6}
    # Row 4
    assert by_type["dash_recent_transactions"]["grid"] == {"x": 0, "y": 19, "w": 12, "h": 11}


# Minimum grid heights (in rows) each default tile needs so its default
# content is fully visible under the card's overflow-hidden. The canvas
# renders a tile at h*60 + (h-1)*12 px (rowHeight 60 + 12px margin). These
# floors back the Reset-to-default "accommodate all the original information"
# contract; the seed may be >= these but never below.
_MIN_CONTENT_H = {
    "dash_on_track": 4,            # 3-stat hero + details link (~216px → 276px)
    "dash_accounts": 9,           # ~8-account list (~456px) with headroom
    "dash_account_forecast": 9,   # eyebrow hero + ~8-row table (~552px)
    "dash_spending": 6,           # donut + ~8-row category legend
    "dash_budget": 6,             # bar chart + header
    "dash_forecast_category": 6,  # bar chart + header
    "dash_recent_transactions": 11,  # 10-row page + header + sort + pager (~714px)
}


@pytest.mark.asyncio
async def test_default_tile_heights_clear_content_floor(session_factory):
    """Every default tile's grid height must be at least its content floor.

    Guards the bug where Reset-to-default produced tiles too small for their
    content (cards cut off; the recent-transactions tile grew an inner
    scrollbar). A future seed tweak that shrinks a tile below its content
    floor must fail here.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard/default")
    assert res.status_code == 200, res.text
    widgets = res.json()["layout_json"]["widgets"]
    by_type = {w["type"]: w for w in widgets}
    for tile_type, min_h in _MIN_CONTENT_H.items():
        assert by_type[tile_type]["grid"]["h"] >= min_h, (
            f"{tile_type} h={by_type[tile_type]['grid']['h']} < content floor {min_h}"
        )


@pytest.mark.asyncio
async def test_default_layout_tiles_do_not_overlap(session_factory):
    """The 7 default tiles must tile the 12-col grid without overlapping.

    Resizing the seed heights (this fix) must keep the rows stacked and
    collision-free so the literal layout reads top-to-bottom as authored.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))
    with TestClient(app) as client:
        res = client.get("/api/v1/dashboard/default")
    assert res.status_code == 200, res.text
    widgets = res.json()["layout_json"]["widgets"]

    # Build the set of occupied (col, row) cells; assert none collide and
    # all stay within the 12-column grid.
    occupied: set[tuple[int, int]] = set()
    for w in widgets:
        g = w["grid"]
        assert g["x"] + g["w"] <= 12, f"{w['type']} overflows 12 cols"
        for col in range(g["x"], g["x"] + g["w"]):
            for row in range(g["y"], g["y"] + g["h"]):
                cell = (col, row)
                assert cell not in occupied, f"{w['type']} overlaps at {cell}"
                occupied.add(cell)


# ─── (i) PATCH accepts dash_* layout → 200; round-trips verbatim ─────────────


@pytest.mark.asyncio
async def test_patch_accepts_dash_widget_types(session_factory):
    """PATCH with a layout containing dash_* widget types must return 200.

    The dashboard-specific validator accepts dash_* types; the strict
    reports validator does NOT — this test verifies the correct one is wired.
    The response must round-trip the dash_* layout VERBATIM.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    dash_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-on-track",
                "type": "dash_on_track",
                "title": "On Track",
                "grid": {"x": 0, "y": 0, "w": 12, "h": 3},
                "config": {},
            },
            {
                "id": "w-accounts",
                "type": "dash_accounts",
                "title": "Accounts",
                "grid": {"x": 0, "y": 3, "w": 4, "h": 5},
                "config": {},
            },
            {
                "id": "w-forecast",
                "type": "dash_account_forecast",
                "title": "Month-End Forecast",
                "grid": {"x": 4, "y": 3, "w": 8, "h": 5},
                "config": {},
            },
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": dash_layout})
    assert res.status_code == 200, res.text
    assert res.json()["layout_json"] == dash_layout


# ─── (j) PATCH rejects unknown widget type → 422 ─────────────────────────────


@pytest.mark.asyncio
async def test_patch_rejects_unknown_widget_type(session_factory):
    """PATCH with an unknown/unsupported widget type must be rejected with 422.

    The dashboard validator accepts only the known dash_* types and the
    known report widget types; any other ``type`` value is a 422.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    bad_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-bad",
                "type": "totally_unknown_widget",
                "title": "Bad",
                "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                "config": {},
            }
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": bad_layout})
    assert res.status_code == 422, res.text


# ─── (m) PATCH accepts 3 chart tile types; round-trips verbatim ───────────────


@pytest.mark.asyncio
async def test_patch_accepts_chart_tile_types(session_factory):
    """PATCH with the 3 Phase-2b chart tiles (dash_spending, dash_budget,
    dash_forecast_category) must return 200 and round-trip the layout VERBATIM.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    chart_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-spending",
                "type": "dash_spending",
                "title": "Spending by Category",
                "grid": {"x": 0, "y": 8, "w": 4, "h": 5},
                "config": {},
            },
            {
                "id": "w-budget",
                "type": "dash_budget",
                "title": "Budget Progress",
                "grid": {"x": 4, "y": 8, "w": 4, "h": 5},
                "config": {},
            },
            {
                "id": "w-forecast-cat",
                "type": "dash_forecast_category",
                "title": "Forecast by Category",
                "grid": {"x": 8, "y": 8, "w": 4, "h": 5},
                "config": {},
            },
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": chart_layout})
    assert res.status_code == 200, res.text
    assert res.json()["layout_json"] == chart_layout


@pytest.mark.asyncio
async def test_patch_accepts_recent_transactions_tile(session_factory):
    """PATCH with the Phase-2c dash_recent_transactions tile must return 200
    and round-trip the layout VERBATIM.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    recent_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-recent",
                "type": "dash_recent_transactions",
                "title": "Recent Transactions",
                "grid": {"x": 0, "y": 13, "w": 12, "h": 6},
                "config": {},
            },
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": recent_layout})
    assert res.status_code == 200, res.text
    assert res.json()["layout_json"] == recent_layout


@pytest.mark.asyncio
async def test_patch_accepts_cc_utilization_tile(session_factory):
    """PATCH with the dash_cc_utilization tile must return 200 and round-trip
    the layout VERBATIM.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    cc_utilization_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-cc-utilization",
                "type": "dash_cc_utilization",
                "title": "Credit Card Utilization",
                "grid": {"x": 0, "y": 19, "w": 4, "h": 5},
                "config": {},
            },
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard", json={"layout_json": cc_utilization_layout}
        )
    assert res.status_code == 200, res.text
    assert res.json()["layout_json"] == cc_utilization_layout


@pytest.mark.asyncio
async def test_patch_still_rejects_unknown_type_after_chart_tiles(session_factory):
    """After adding the 3 chart tiles, unknown widget types must still 422."""
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    bad_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-bad",
                "type": "dash_unknown_tile",
                "title": "Bad",
                "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                "config": {},
            }
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": bad_layout})
    assert res.status_code == 422, res.text


# ─── (l) PATCH rejects empty widget title → 422 ─────────────────────────────


@pytest.mark.asyncio
async def test_patch_rejects_empty_widget_title(session_factory):
    """PATCH with a widget whose title is an empty string must be rejected with 422.

    _DashWidgetBase.title uses Field(min_length=1); a blank title is
    structurally invalid for both dash_* and report widget types.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    empty_title_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-empty-title",
                "type": "dash_on_track",
                "title": "",  # empty string — must be rejected
                "grid": {"x": 0, "y": 0, "w": 12, "h": 3},
                "config": {},
            }
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard", json={"layout_json": empty_title_layout}
        )
    assert res.status_code == 422, res.text


# ─── (k) PATCH rejects malformed grid (w=0) → 422 ────────────────────────────


@pytest.mark.asyncio
async def test_patch_rejects_zero_width_grid(session_factory):
    """PATCH with a widget whose grid.w=0 must be rejected with 422.

    WidgetGrid.w is Field(gt=0); a zero-width widget is structurally invalid
    regardless of the widget type.
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    bad_grid_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "w-bad-grid",
                "type": "dash_on_track",
                "title": "On Track",
                "grid": {"x": 0, "y": 0, "w": 0, "h": 3},  # w=0 is invalid
                "config": {},
            }
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch(
            "/api/v1/dashboard", json={"layout_json": bad_grid_layout}
        )
    assert res.status_code == 422, res.text


# ─── (n) PATCH accepts cloned sankey widget; round-trips verbatim ────────────


@pytest.mark.asyncio
async def test_patch_accepts_cloned_sankey_widget(session_factory):
    """PATCH with a cloned sankey widget must return 200 and round-trip the
    layout VERBATIM, including sankey-specific knobs (top_n, spending_granularity).
    """
    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    sankey_layout = {
        "version": 1,
        "widgets": [
            {
                "id": "s1",
                "type": "sankey",
                "title": "Cash Flow",
                "grid": {"x": 0, "y": 0, "w": 8, "h": 5},
                "config": {
                    "dataset": "transactions",
                    "measure": {"agg": "sum", "field": "amount"},
                    "spending_granularity": "category",
                    "top_n": 12,
                },
            }
        ],
    }

    with TestClient(app) as client:
        client.get("/api/v1/dashboard")
        res = client.patch("/api/v1/dashboard", json={"layout_json": sankey_layout})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["layout_json"] == sankey_layout
    assert body["layout_json"]["widgets"][0]["config"]["top_n"] == 12
    assert body["layout_json"]["widgets"][0]["config"]["spending_granularity"] == "category"


# ─── (o) GET /default returns seed without persisting a row ──────────────────


@pytest.mark.asyncio
async def test_get_default_returns_seed_without_persisting(session_factory):
    """GET /api/v1/dashboard/default returns the 7-tile canonical seed with 200
    and does NOT create a DashboardLayout row for a user who has never
    GET/PATCHed the normal endpoint.
    """
    from sqlalchemy import func, select as _s

    from app.models.dashboard import DashboardLayout

    await _seed(session_factory)
    app = _make_app(session_factory, _resolver("user_a"))

    with TestClient(app) as client:
        r = client.get("/api/v1/dashboard/default")
    assert r.status_code == 200, r.text

    body = r.json()
    types = [w["type"] for w in body["layout_json"]["widgets"]]
    assert "dash_on_track" in types
    assert len(types) == 7

    # The /default endpoint must NOT have created a DashboardLayout row.
    async with session_factory() as db:
        count = (
            await db.execute(_s(func.count()).select_from(DashboardLayout))
        ).scalar_one()
    assert count == 0, f"Expected 0 DashboardLayout rows, got {count}"
