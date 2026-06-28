/**
 * Dashboard widget default-grid sizing guard.
 *
 * The Reset-to-default flow pulls the canonical 7-tile layout from the backend
 * seed (routers/dashboard.py DEFAULT_DASHBOARD_LAYOUT). The frontend
 * DASHBOARD_WIDGET_DEFAULTS (widget-types.ts) backs the per-type "Add widget"
 * placement and MUST stay in sync with that seed — a drift between the two is
 * itself a bug (the seed comment says "Keep in sync").
 *
 * These tests pin the frontend defaults to the same grid values the backend
 * seed test (test_dashboard.py::test_default_layout_contains_seven_dash_tiles)
 * pins, and assert each tile clears its content-height floor so a default /
 * freshly-added tile is never too small for its content (cards cut off; the
 * recent-transactions tile growing an inner scrollbar).
 */
import { describe, expect, it } from "vitest";

import {
  emptyDashboardWidget,
  type DashboardWidgetType,
} from "@/lib/dashboard/widget-types";
import type { WidgetGrid } from "@/lib/reports/types";

/**
 * Canonical default grids — MUST equal the backend DEFAULT_DASHBOARD_LAYOUT.
 * If you change one side, change the other (and its test).
 */
const CANONICAL_GRIDS: Record<DashboardWidgetType, WidgetGrid> = {
  dash_on_track: { x: 0, y: 0, w: 12, h: 4 },
  dash_accounts: { x: 0, y: 4, w: 4, h: 9 },
  dash_account_forecast: { x: 4, y: 4, w: 8, h: 9 },
  dash_spending: { x: 0, y: 13, w: 4, h: 6 },
  dash_budget: { x: 4, y: 13, w: 4, h: 6 },
  dash_forecast_category: { x: 8, y: 13, w: 4, h: 6 },
  dash_recent_transactions: { x: 0, y: 19, w: 12, h: 11 },
};

/**
 * Minimum grid heights (rows) each tile needs so its default content is fully
 * visible under the card's overflow-hidden. Canvas renders a tile at
 * h*60 + (h-1)*12 px (rowHeight 60 + 12px margin).
 */
const MIN_CONTENT_H: Record<DashboardWidgetType, number> = {
  dash_on_track: 4,
  dash_accounts: 9,
  dash_account_forecast: 9,
  dash_spending: 6,
  dash_budget: 6,
  dash_forecast_category: 6,
  dash_recent_transactions: 11,
};

describe("dashboard widget default grids", () => {
  // NOTE: CANONICAL_GRIDS is a hand-kept MIRROR of the backend
  // DEFAULT_DASHBOARD_LAYOUT — the two are separate constants (Python vs TS)
  // with no shared source, so this asserts the frontend matches that mirror,
  // not the live backend. Cross-stack parity is enforced by the "Keep in
  // sync" comments + the backend counterpart
  // (test_dashboard.py::test_default_layout_contains_seven_dash_tiles); a
  // backend-only edit would NOT fail this test. Keep both sides equal by hand.
  it("equal the hand-mirrored backend seed grids", () => {
    for (const type of Object.keys(CANONICAL_GRIDS) as DashboardWidgetType[]) {
      const w = emptyDashboardWidget(type, "test-id");
      expect(w.grid, `${type} grid`).toEqual(CANONICAL_GRIDS[type]);
    }
  });

  it("size every tile at or above its content-height floor", () => {
    for (const type of Object.keys(MIN_CONTENT_H) as DashboardWidgetType[]) {
      const w = emptyDashboardWidget(type, "test-id");
      expect(
        w.grid.h,
        `${type} h=${w.grid.h} below content floor ${MIN_CONTENT_H[type]}`,
      ).toBeGreaterThanOrEqual(MIN_CONTENT_H[type]);
    }
  });

  it("place the recent-transactions tile tall enough for the 10-row page", () => {
    // The default page size is 10. At rowHeight 60 + 12px margin the tile is
    // h*60 + (h-1)*12 px; h=11 → 780px clears the ~714px the 10-row table +
    // header + sort row + pager need, so no inner scrollbar appears on reset.
    const w = emptyDashboardWidget("dash_recent_transactions", "rt");
    const px = w.grid.h * 60 + (w.grid.h - 1) * 12;
    expect(px).toBeGreaterThanOrEqual(714);
  });
});
