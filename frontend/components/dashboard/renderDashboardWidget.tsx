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
import SpendingDonutWidget from "@/components/dashboard/widgets/SpendingDonutWidget";
import BudgetBarsWidget from "@/components/dashboard/widgets/BudgetBarsWidget";
import ForecastBarsWidget from "@/components/dashboard/widgets/ForecastBarsWidget";
import RecentTransactionsWidget from "@/components/dashboard/widgets/RecentTransactionsWidget";
import CreditUtilizationWidget from "@/components/dashboard/widgets/CreditUtilizationWidget";
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
  // dash_* tiles render content-height cards. On the canvas, react-grid-layout
  // gives each widget a FIXED-height box (h * rowHeight + margins) and draws the
  // resize handle at that box's corner. Wrap each tile so its card fills the box:
  // `h-full` makes the wrapper fill WidgetShell's flex body, and `[&>*]:h-full`
  // forces the tile's single root card to fill the wrapper. Without this the card
  // collapses to content height — the resize handle floats below it and tiles
  // space inconsistently. Report-cloned widgets already fill via their own
  // `h-full` roots, so the default arm bypasses this wrapper.
  const fill = (tile: ReactNode): ReactNode => (
    <div className="h-full [&>*]:h-full">{tile}</div>
  );

  switch (w.type) {
    // ── Dashboard-native tiles ──────────────────────────────────────────────
    case "dash_on_track":
      return fill(<OnTrackWidget />);

    case "dash_accounts":
      return fill(<AccountsWidget />);

    case "dash_account_forecast":
      return fill(<AccountForecastWidget />);

    case "dash_spending":
      return fill(<SpendingDonutWidget />);

    case "dash_budget":
      return fill(<BudgetBarsWidget />);

    case "dash_forecast_category":
      return fill(<ForecastBarsWidget />);

    case "dash_recent_transactions":
      return fill(<RecentTransactionsWidget />);

    case "dash_cc_utilization":
      return fill(<CreditUtilizationWidget />);

    // ── Reports fall-through (cloned report widgets) ────────────────────────
    // Delegate all non-dash types to the shared report widget renderer.
    // ``renderReportWidget`` includes a sankey arm, and since Task 1 of Phase 3
    // added "sankey" to the dashboard layout validator, a sankey widget CAN now
    // be cloned onto a dashboard layout and WILL reach this arm via the fall-
    // through. The reports strict validator still rejects ``dash_*`` types.
    default:
      return renderReportWidget(w as Widget, canvasFilters, editMode, currency) ?? null;
  }
}
