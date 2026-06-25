"use client";

/**
 * renderReportWidget — single-source renderer for all report widget types.
 *
 * Previously duplicated as a module-private ``renderWidgetByType`` in
 * ``app/reports/[id]/page.tsx`` AND an inline fall-through switch in
 * ``renderDashboardWidget.tsx``.  Both call sites now import this function.
 *
 * Includes the ``sankey`` arm so the reports editor continues to render
 * SankeyWidget.  The dashboard fall-through routes non-dash types here too,
 * but a sankey widget can never be persisted on a dashboard layout (the
 * backend WidgetType enum rejects it), so the arm is safe to include — it
 * simply will never be reached from the dashboard path.
 */
import type { ReactNode } from "react";

import type { CanvasFilters, Widget } from "@/lib/reports/types";

import KPIWidget from "@/components/reports/widgets/KPIWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import StackedBarWidget from "@/components/reports/widgets/StackedBarWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import SankeyWidget from "@/components/reports/widgets/SankeyWidget";

/**
 * Render a report widget by its type.
 *
 * @param w             - The widget to render.
 * @param canvasFilters - Canvas-wide date/account/category filters.
 * @param editMode      - Whether the canvas is in edit mode.
 * @param currency      - Display currency symbol (from org accounts).
 */
export function renderReportWidget(
  w: Widget,
  canvasFilters: CanvasFilters,
  editMode: boolean,
  currency?: string,
): ReactNode {
  switch (w.type) {
    case "kpi":
      return (
        <KPIWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "bar":
      return (
        <BarWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "line":
      return (
        <LineWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "area":
      return (
        <AreaWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "pie":
      return (
        <PieWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "sparkline":
      return (
        <SparklineWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "stacked_bar":
      return (
        <StackedBarWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "table":
      return (
        <TableWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
    case "sankey":
      return (
        <SankeyWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
          currency={currency}
        />
      );
  }
}
