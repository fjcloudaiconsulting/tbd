/**
 * Dashboard widget types — dashboard-scoped widget kinds + factory.
 *
 * Dashboard widgets reuse the reports ``BaseWidget`` shape
 * ({id,type,title,grid,config}), but their ``type`` discriminant is a
 * dashboard-specific string.  Data comes entirely from the
 * ``DashboardDataProvider`` context, so ``config`` is always ``{}``.
 *
 * The ``DashboardWidget`` union is intentionally separate from the
 * reports ``Widget`` union.  ``renderDashboardWidget`` handles both
 * by delegating unknown types to the reports renderer.
 */

import type { WidgetGrid } from "@/lib/reports/types";

/** All dashboard-specific widget type discriminants. */
export type DashboardWidgetType =
  | "dash_on_track"
  | "dash_accounts"
  | "dash_account_forecast"
  | "dash_spending"
  | "dash_budget"
  | "dash_forecast_category"
  | "dash_recent_transactions"
  | "dash_cc_utilization";

/** A dashboard-native widget.  config is empty — the provider owns the data. */
export interface DashboardWidget {
  id: string;
  type: DashboardWidgetType;
  title: string;
  grid: WidgetGrid;
  config: Record<string, never>;
}

// Default grid placement + size for each dashboard tile. Heights are sized so
// a tile shows ALL its default content WITHOUT the card's `overflow-hidden`
// clipping it. The canvas renders each tile at `h*60 + (h-1)*12` px (rowHeight
// 60 + 12px row margin — see components/reports/Canvas.tsx). These MUST stay in
// sync with the backend DEFAULT_DASHBOARD_LAYOUT in routers/dashboard.py — that
// backend seed is the source of truth for Reset-to-default; this table backs the
// per-type "Add widget" placement, and a test pins the two to identical grids.
const DASHBOARD_WIDGET_DEFAULTS: Record<
  DashboardWidgetType,
  { title: string; grid: WidgetGrid }
> = {
  dash_on_track: {
    title: "On Track",
    // h=4 (~276px) clears the 3-stat hero + "View details" link (~216px).
    grid: { x: 0, y: 0, w: 12, h: 4 },
  },
  dash_accounts: {
    title: "Accounts",
    // h=9 (~636px) fits an ~8-account list (~57px/row) without clipping.
    grid: { x: 0, y: 4, w: 4, h: 9 },
  },
  dash_account_forecast: {
    title: "Month-End Forecast",
    // h=9 (~636px) fits the eyebrow hero + ~8-row month-end table (~552px).
    grid: { x: 4, y: 4, w: 8, h: 9 },
  },
  dash_spending: {
    title: "Spending by Category",
    // h=6 (~420px) fits the 160px donut beside a ~8-row category legend.
    grid: { x: 0, y: 13, w: 4, h: 6 },
  },
  dash_budget: {
    title: "Budget Progress",
    grid: { x: 4, y: 13, w: 4, h: 6 },
  },
  dash_forecast_category: {
    title: "Forecast by Category",
    grid: { x: 8, y: 13, w: 4, h: 6 },
  },
  dash_recent_transactions: {
    title: "Recent Transactions",
    // h=11 (~780px) fits the 10-row default page + header + sort row + pager
    // (~714px) without an inner scrollbar; the row region scrolls if resized
    // smaller. Keep in sync with the backend DEFAULT_DASHBOARD_LAYOUT.
    grid: { x: 0, y: 19, w: 12, h: 11 },
  },
  dash_cc_utilization: {
    title: "Credit card utilization",
    grid: { x: 0, y: 25, w: 4, h: 6 },
  },
};

/**
 * Factory for a dashboard widget with sane default grid and config.
 *
 * @param type - The dashboard widget type discriminant.
 * @param id   - A stable unique string ID (e.g. from ``crypto.randomUUID()``).
 */
export function emptyDashboardWidget(
  type: DashboardWidgetType,
  id: string,
): DashboardWidget {
  const defaults = DASHBOARD_WIDGET_DEFAULTS[type];
  return {
    id,
    type,
    title: defaults.title,
    grid: { ...defaults.grid },
    config: {},
  };
}
