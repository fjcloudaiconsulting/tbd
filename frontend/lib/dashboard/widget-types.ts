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
  | "dash_recent_transactions";

/** A dashboard-native widget.  config is empty — the provider owns the data. */
export interface DashboardWidget {
  id: string;
  type: DashboardWidgetType;
  title: string;
  grid: WidgetGrid;
  config: Record<string, never>;
}

const DASHBOARD_WIDGET_DEFAULTS: Record<
  DashboardWidgetType,
  { title: string; grid: WidgetGrid }
> = {
  dash_on_track: {
    title: "On Track",
    grid: { x: 0, y: 0, w: 12, h: 3 },
  },
  dash_accounts: {
    title: "Accounts",
    grid: { x: 0, y: 3, w: 4, h: 5 },
  },
  dash_account_forecast: {
    title: "Month-End Forecast",
    grid: { x: 4, y: 3, w: 8, h: 5 },
  },
  dash_spending: {
    title: "Spending by Category",
    grid: { x: 0, y: 8, w: 4, h: 5 },
  },
  dash_budget: {
    title: "Budget Progress",
    grid: { x: 4, y: 8, w: 4, h: 5 },
  },
  dash_forecast_category: {
    title: "Forecast by Category",
    grid: { x: 8, y: 8, w: 4, h: 5 },
  },
  dash_recent_transactions: {
    title: "Recent Transactions",
    grid: { x: 0, y: 13, w: 12, h: 6 },
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
