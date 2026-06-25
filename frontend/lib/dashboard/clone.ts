import { newWidgetId } from "../../components/reports/widgetKit";
import type { Widget } from "../reports/types";
import type { DashboardWidget } from "./widget-types";

/**
 * Clone a report widget for placement on the dashboard. The clone is fully
 * independent (deep copy, fresh id) and self-fetches via useReportQuery — no
 * linkage back to the source report. Grid keeps the source w/h and drops to
 * the first free row below every existing widget.
 */
export function cloneWidgetForDashboard(
  source: Widget,
  existing: Array<Widget | DashboardWidget>,
): Widget {
  const copy: Widget = JSON.parse(JSON.stringify(source));
  const maxY = existing.reduce((m, w) => Math.max(m, w.grid.y + w.grid.h), 0);
  copy.id = newWidgetId();
  copy.grid = { x: 0, y: maxY, w: source.grid.w, h: source.grid.h };
  return copy;
}
