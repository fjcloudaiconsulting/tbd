"use client";

/**
 * renderDashboardWidget — dispatch function for the custom dashboard canvas.
 *
 * Handles dashboard-native widget types (``dash_*``) by rendering the
 * corresponding provider-fed tile wrapper.  All other types fall through to
 * the reports widget renderer so report-cloned widgets continue to work on
 * the dashboard without any extra wiring.
 *
 * ``renderWidgetByType`` is a module-private helper in the reports page and
 * in ``CustomDashboard``.  Rather than exporting it from a Next.js page
 * (which is awkward — page modules export a default component; named exports
 * are reserved for Next.js metadata/route conventions), we replicate the
 * minimal switch here alongside the dashboard-specific cases.  Both copies
 * stay in sync via the shared widget component imports.
 */
import type { ReactNode } from "react";

import type { CanvasFilters, Widget } from "@/lib/reports/types";
import type { DashboardWidget } from "@/lib/dashboard/widget-types";

import OnTrackWidget from "@/components/dashboard/widgets/OnTrackWidget";
import AccountsWidget from "@/components/dashboard/widgets/AccountsWidget";
import AccountForecastWidget from "@/components/dashboard/widgets/AccountForecastWidget";

// Reports widget components — mirror the import list in CustomDashboard so
// the fall-through branch renders identically to the reports surface.
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
 * Render a dashboard canvas widget.
 *
 * @param w             - A dashboard-native widget OR a report-cloned widget.
 * @param canvasFilters - Canvas-wide date filter (only used by report widgets).
 * @param editMode      - Whether the canvas is in edit/customize mode.
 * @param currency      - Display currency for report widgets.
 */
export function renderDashboardWidget(
  w: DashboardWidget | Widget,
  canvasFilters: CanvasFilters = {},
  editMode = false,
  currency?: string,
): ReactNode {
  switch (w.type) {
    // ── Dashboard-native tiles ──────────────────────────────────────────────
    case "dash_on_track":
      return <OnTrackWidget />;

    case "dash_accounts":
      return <AccountsWidget />;

    case "dash_account_forecast":
      return <AccountForecastWidget />;

    // ── Reports fall-through (cloned report widgets) ────────────────────────
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
