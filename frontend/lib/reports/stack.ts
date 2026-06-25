/**
 * Mobile single-column stack helpers — shared between the Reports editor
 * page and the CustomDashboard canvas shell.
 *
 * Extracted from ``app/reports/[id]/page.tsx`` so that components (e.g.
 * ``components/dashboard/CustomDashboard.tsx``) can import these utilities
 * without coupling to a Next.js route module's import graph.
 */
import type { Widget, WidgetType } from "@/lib/reports/types";
import type { DashboardWidget } from "@/lib/dashboard/widget-types";

/**
 * Order widgets for the mobile single-column stack: top-to-bottom by
 * grid ``y``, then left-to-right by grid ``x`` so the vertical reading
 * order matches what the desktop grid shows. Exported for unit testing
 * the ordering independently of viewport mocking.
 *
 * Generic so callers that pass ``Widget[]`` get ``Widget[]`` back, and
 * callers that pass ``(Widget | DashboardWidget)[]`` get the union back —
 * no widening of existing call-sites.
 */
export function orderWidgetsForStack<T extends Widget | DashboardWidget>(widgets: T[]): T[] {
  return [...widgets].sort((a, b) => {
    if (a.grid.y !== b.grid.y) return a.grid.y - b.grid.y;
    return a.grid.x - b.grid.x;
  });
}

// Chart widgets need a definite height for Recharts/Nivo height="100%" to
// render in the mobile stack (the wrapper is otherwise auto-height → ~0).
// KPI and table size to their content, so they stay natural-height.
const CHART_STACK_TYPES = new Set<WidgetType>([
  "bar", "stacked_bar", "line", "area", "pie", "sparkline", "sankey",
]);

// dash_* chart-like tiles embed a Recharts/Nivo chart and need a fixed height
// just like report chart widgets.
const DASH_CHART_TYPES = new Set([
  "dash_spending", "dash_budget", "dash_forecast_category",
]);

// dash_* content tiles (lists, summary cards, transaction table) need enough
// room to show meaningful content without collapsing on mobile.
const DASH_CONTENT_TYPES = new Set([
  "dash_on_track", "dash_accounts", "dash_account_forecast", "dash_recent_transactions",
]);

/**
 * Returns a pixel height for the mobile stack wrapper of a chart widget,
 * so that Recharts/Nivo ``height="100%"`` resolves to a usable size.
 * Returns ``undefined`` for content widgets (kpi, table) that naturally
 * size to their own content.
 *
 * Accepts both report ``Widget`` and dashboard ``DashboardWidget`` types.
 */
export function mobileStackHeight(widget: Widget | DashboardWidget): number | undefined {
  const base = widget.grid.h * 56; // ~rowHeight; taller widgets stay taller

  // dash_* chart tiles: reuse the chart height formula [220, 460]
  if (DASH_CHART_TYPES.has(widget.type)) {
    return Math.min(Math.max(base, 220), 460);
  }

  // dash_* content tiles: clamp to [200, 520] so they don't collapse on mobile
  if (DASH_CONTENT_TYPES.has(widget.type)) {
    return Math.min(Math.max(base, 200), 520);
  }

  // Report chart widgets
  if (!CHART_STACK_TYPES.has(widget.type as WidgetType)) return undefined;
  return Math.min(Math.max(base, widget.type === "sankey" || widget.type === "pie" ? 260 : 220), 460);
}
