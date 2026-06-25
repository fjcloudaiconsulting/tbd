"use client";

/**
 * AddWidgetMenu — picker for adding widgets to the custom dashboard.
 *
 * Shows a "Dashboard tiles" group (the 7 dash_* finance tiles) so users
 * can re-add any tile they previously removed. A "From a report" section
 * header is included as an inert placeholder; Task 4 will implement that
 * path and will use the same `onAddCloned` prop.
 *
 * Modelled on `components/reports/WidgetPicker.tsx` (same grouped option
 * grid, token classes only — no raw Tailwind palette colors).
 */
import {
  Activity,
  ArrowRightLeft,
  BarChart3,
  CircleDollarSign,
  CreditCard,
  LayoutDashboard,
  TrendingUp,
} from "lucide-react";

import type { DashboardWidget, DashboardWidgetType } from "@/lib/dashboard/widget-types";
import type { Widget } from "@/lib/reports/types";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Widgets already present on the canvas (used by future de-dupe logic). */
  existing: Array<Widget | DashboardWidget>;
  /** Called when the user picks a dash_* tile to add. */
  onAddDashTile: (type: DashboardWidgetType) => void;
  /**
   * Called when the user picks a report widget to clone onto the dashboard.
   * Implemented in Task 4; pass a no-op for now.
   */
  onAddCloned: (w: Widget) => void;
}

interface DashOption {
  type: DashboardWidgetType;
  label: string;
  description: string;
  Icon: typeof BarChart3;
}

const DASH_TILES: DashOption[] = [
  {
    type: "dash_on_track",
    label: "On track",
    description: "Budget health verdict for the current period.",
    Icon: Activity,
  },
  {
    type: "dash_accounts",
    label: "Accounts",
    description: "Live balances across all active accounts.",
    Icon: CreditCard,
  },
  {
    type: "dash_account_forecast",
    label: "Account forecast",
    description: "Projected month-end balance per account.",
    Icon: TrendingUp,
  },
  {
    type: "dash_spending",
    label: "Spending",
    description: "Spending by category for the current period.",
    Icon: CircleDollarSign,
  },
  {
    type: "dash_budget",
    label: "Budget",
    description: "Budget progress bars for the current period.",
    Icon: LayoutDashboard,
  },
  {
    type: "dash_forecast_category",
    label: "Forecast by category",
    description: "Planned vs actual by category from the forecast plan.",
    Icon: BarChart3,
  },
  {
    type: "dash_recent_transactions",
    label: "Recent transactions",
    description: "Paginated transaction list for the current period.",
    Icon: ArrowRightLeft,
  },
];

export default function AddWidgetMenu({
  open,
  onClose,
  onAddDashTile,
}: Props) {
  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Add widget"
      data-testid="add-widget-menu"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 px-4"
    >
      <div className="w-full max-w-2xl rounded-lg border border-border bg-surface p-5 shadow-xl">
        {/* Header */}
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold text-text-primary">
            Add widget
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-text-muted hover:text-text-primary"
            aria-label="Close"
          >
            Close
          </button>
        </div>

        <div className="space-y-4">
          {/* ── Dashboard tiles group ─────────────────────────────────────── */}
          <div data-testid="add-widget-menu-group-dashboard">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
              Dashboard tiles
            </div>
            <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {DASH_TILES.map(({ type, label, description, Icon }) => (
                <li key={type}>
                  <button
                    type="button"
                    onClick={() => onAddDashTile(type)}
                    data-testid={`add-widget-menu-option-${type}`}
                    className="flex w-full items-start gap-3 rounded-md border border-border bg-bg p-3 text-left transition hover:border-accent hover:bg-surface-raised"
                  >
                    <Icon
                      aria-hidden="true"
                      className="mt-0.5 h-5 w-5 text-accent"
                      strokeWidth={1.5}
                    />
                    <div>
                      <div className="text-sm font-medium text-text-primary">
                        {label}
                      </div>
                      <div className="text-xs text-text-muted">
                        {description}
                      </div>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {/* ── From a report (placeholder — implemented in Task 4) ──────── */}
          <div data-testid="add-widget-menu-group-report">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
              From a report
            </div>
            <p className="text-xs text-text-muted">
              Clone a saved report widget onto your dashboard. (Coming soon.)
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
