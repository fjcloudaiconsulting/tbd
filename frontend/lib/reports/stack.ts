/**
 * Mobile single-column stack helpers — shared between the Reports editor
 * page and the CustomDashboard canvas shell.
 *
 * Extracted from ``app/reports/[id]/page.tsx`` so that components (e.g.
 * ``components/dashboard/CustomDashboard.tsx``) can import these utilities
 * without coupling to a Next.js route module's import graph.
 */
import type { Widget, WidgetType } from "@/lib/reports/types";

/**
 * Order widgets for the mobile single-column stack: top-to-bottom by
 * grid ``y``, then left-to-right by grid ``x`` so the vertical reading
 * order matches what the desktop grid shows. Exported for unit testing
 * the ordering independently of viewport mocking.
 */
export function orderWidgetsForStack(widgets: Widget[]): Widget[] {
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

/**
 * Returns a pixel height for the mobile stack wrapper of a chart widget,
 * so that Recharts/Nivo ``height="100%"`` resolves to a usable size.
 * Returns ``undefined`` for content widgets (kpi, table) that naturally
 * size to their own content.
 */
export function mobileStackHeight(widget: Widget): number | undefined {
  if (!CHART_STACK_TYPES.has(widget.type)) return undefined;
  const base = widget.grid.h * 56; // ~rowHeight; taller widgets stay taller
  return Math.min(Math.max(base, widget.type === "sankey" || widget.type === "pie" ? 260 : 220), 460);
}
