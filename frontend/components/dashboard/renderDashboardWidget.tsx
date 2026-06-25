"use client";

/**
 * renderDashboardWidget — dispatch function for the custom dashboard canvas.
 *
 * Handles dashboard-native widget types (``dash_*``) by rendering the
 * corresponding provider-fed tile wrapper.  All other types fall through to
 * the reports widget renderer so report-cloned widgets continue to work on
 * the dashboard without any extra wiring.
 *
 * All non-dash widget types fall through to ``renderReportWidget`` from
 * ``components/reports/renderReportWidget.tsx``, which is the single source
 * of truth for the report widget switch.  The dashboard-native ``dash_*``
 * arms are handled first; everything else delegates to the shared renderer.
 */
import type { ReactNode } from "react";

import type { CanvasFilters, Widget } from "@/lib/reports/types";
import type { DashboardWidget } from "@/lib/dashboard/widget-types";

import OnTrackWidget from "@/components/dashboard/widgets/OnTrackWidget";
import AccountsWidget from "@/components/dashboard/widgets/AccountsWidget";
import AccountForecastWidget from "@/components/dashboard/widgets/AccountForecastWidget";
import { renderReportWidget } from "@/components/reports/renderReportWidget";
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
    // Delegate all non-dash types to the shared report widget renderer.
    // ``renderReportWidget`` includes a sankey arm, but a sankey widget can
    // never be persisted on a dashboard layout (the backend WidgetType enum
    // rejects it), so that arm is unreachable from this path — it is safe to
    // route through a sankey-capable renderer.
    default:
      return renderReportWidget(w as Widget, canvasFilters, editMode, currency) ?? null;
  }
}
