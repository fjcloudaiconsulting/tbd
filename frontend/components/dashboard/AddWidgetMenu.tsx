"use client";

/**
 * AddWidgetMenu — picker for adding widgets to the custom dashboard.
 *
 * Shows a "Dashboard tiles" group (the 7 dash_* finance tiles) so users
 * can re-add any tile they previously removed, and a "From a report" entry
 * that lets users clone a widget from any saved report onto the dashboard.
 *
 * Internal view state: "root" | "reports" | "widgets"
 *   root    → the default landing showing dash tiles + "From a report" button
 *   reports → list of the user's saved reports (fetched on entry)
 *   widgets → list of widgets in the selected report
 *
 * Modelled on `components/reports/WidgetPicker.tsx` (same grouped option
 * grid, token classes only — no raw Tailwind palette colors).
 *
 * A11y (WCAG 2.2 AA): Escape closes the menu via a document keydown listener
 * (mirrors ConfirmModal pattern). Backdrop click closes via onClick on the
 * outer overlay; inner-panel clicks do NOT propagate to it.
 */
import { useEffect, useState } from "react";
import {
  Activity,
  ArrowLeft,
  ArrowRightLeft,
  BarChart3,
  CircleDollarSign,
  CreditCard,
  LayoutDashboard,
  TrendingUp,
} from "lucide-react";

import { cloneWidgetForDashboard } from "@/lib/dashboard/clone";
import { listReports } from "@/lib/reports/api";
import type { ReportSummary, Widget } from "@/lib/reports/types";
import type { DashboardWidget, DashboardWidgetType } from "@/lib/dashboard/widget-types";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Widgets already present on the canvas — used by cloneWidgetForDashboard for placement. */
  existing: Array<Widget | DashboardWidget>;
  /** Called when the user picks a dash_* tile to add. */
  onAddDashTile: (type: DashboardWidgetType) => void;
  /**
   * Called when the user picks a report widget to clone onto the dashboard.
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

/** Convert a widget type slug to a human-readable fallback label. */
function humanizeWidgetType(type: string): string {
  return type
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Slugify a report name for use in a test-id (lowercase, spaces→hyphens). */
function slugifyName(name: string): string {
  return name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
}

type View = "root" | "reports" | "widgets";

export default function AddWidgetMenu({
  open,
  onClose,
  existing,
  onAddDashTile,
  onAddCloned,
}: Props) {
  const [view, setView] = useState<View>("root");
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [reportsLoading, setReportsLoading] = useState(false);
  const [selectedReport, setSelectedReport] = useState<ReportSummary | null>(null);

  // Reset sub-view when the menu closes.
  useEffect(() => {
    if (!open) {
      setView("root");
      setSelectedReport(null);
      setReports([]);
    }
  }, [open]);

  // Escape closes the menu — mirrors ConfirmModal's document-listener pattern.
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        if (view !== "root") {
          setView("root");
          setSelectedReport(null);
        } else {
          onClose();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onClose, view]);

  if (!open) return null;

  /** Enter the "From a report" sub-flow: fetch reports. */
  function enterReports() {
    setView("reports");
    setReportsLoading(true);
    listReports()
      .then((data) => {
        setReports(data);
      })
      .finally(() => {
        setReportsLoading(false);
      });
  }

  /** Select a report and show its widget list. */
  function selectReport(report: ReportSummary) {
    setSelectedReport(report);
    setView("widgets");
  }

  /** Clone the chosen widget and hand it back to the parent. */
  function pickWidget(widget: Widget) {
    onAddCloned(cloneWidgetForDashboard(widget, existing));
  }

  /** Widgets in the selected report, guarding the empty-layout case. */
  function selectedWidgets(): Widget[] {
    if (!selectedReport) return [];
    const lj = selectedReport.layout_json;
    if (!lj || !("widgets" in lj) || !Array.isArray((lj as { widgets: unknown }).widgets)) {
      return [];
    }
    return (lj as { widgets: Widget[] }).widgets;
  }

  // ── Shared panel chrome ─────────────────────────────────────────────────────
  const panelHeader = (
    <div className="mb-4 flex items-center justify-between">
      <div className="flex items-center gap-2">
        {view !== "root" && (
          <button
            type="button"
            aria-label="Back"
            onClick={() => {
              if (view === "widgets") {
                setView("reports");
                setSelectedReport(null);
              } else {
                setView("root");
              }
            }}
            className="flex items-center gap-1 text-sm text-text-muted hover:text-text-primary"
          >
            <ArrowLeft aria-hidden="true" className="h-4 w-4" />
            Back
          </button>
        )}
        <h2 className="text-base font-semibold text-text-primary">
          {view === "root" && "Add widget"}
          {view === "reports" && "From a report"}
          {view === "widgets" && (selectedReport?.name ?? "Widgets")}
        </h2>
      </div>
      <button
        type="button"
        onClick={onClose}
        className="text-sm text-text-muted hover:text-text-primary"
        aria-label="Close"
      >
        Close
      </button>
    </div>
  );

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Add widget"
      data-testid="add-widget-menu"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 px-4"
      onClick={onClose}
    >
      {/* stopPropagation prevents clicks inside the panel from bubbling to the backdrop */}
      <div
        className="w-full max-w-2xl rounded-lg border border-border bg-surface p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {panelHeader}

        {/* ── Root view ──────────────────────────────────────────────────────── */}
        {view === "root" && (
          <div className="space-y-4">
            {/* Dashboard tiles group */}
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

            {/* From a report entry */}
            <div data-testid="add-widget-menu-group-report">
              <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
                From a report
              </div>
              <button
                type="button"
                onClick={enterReports}
                className="flex w-full items-center gap-3 rounded-md border border-border bg-bg p-3 text-left transition hover:border-accent hover:bg-surface-raised"
              >
                <BarChart3
                  aria-hidden="true"
                  className="h-5 w-5 text-accent"
                  strokeWidth={1.5}
                />
                <div>
                  <div className="text-sm font-medium text-text-primary">
                    From a report
                  </div>
                  <div className="text-xs text-text-muted">
                    Clone a saved report widget onto your dashboard.
                  </div>
                </div>
              </button>
            </div>
          </div>
        )}

        {/* ── Reports list view ───────────────────────────────────────────────── */}
        {view === "reports" && (
          <div>
            {reportsLoading ? (
              <div className="flex justify-center py-6">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-accent" />
              </div>
            ) : reports.length === 0 ? (
              <p className="py-4 text-sm text-text-muted">
                You have no saved reports yet.
              </p>
            ) : (
              <ul className="space-y-2">
                {reports.map((report) => (
                  <li key={report.id}>
                    <button
                      type="button"
                      data-testid={`add-widget-menu-report-${slugifyName(report.name)}`}
                      onClick={() => selectReport(report)}
                      className="flex w-full items-center rounded-md border border-border bg-bg px-4 py-3 text-left text-sm font-medium text-text-primary transition hover:border-accent hover:bg-surface-raised"
                    >
                      {report.name}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* ── Widget list view ────────────────────────────────────────────────── */}
        {view === "widgets" && (
          <div>
            {(() => {
              const widgets = selectedWidgets();
              if (widgets.length === 0) {
                return (
                  <p className="py-4 text-sm text-text-muted">
                    This report has no widgets.
                  </p>
                );
              }
              return (
                <ul className="space-y-2">
                  {widgets.map((widget) => (
                    <li key={widget.id}>
                      <button
                        type="button"
                        data-testid={`add-widget-menu-report-widget-${widget.id}`}
                        onClick={() => pickWidget(widget)}
                        className="flex w-full items-center rounded-md border border-border bg-bg px-4 py-3 text-left text-sm font-medium text-text-primary transition hover:border-accent hover:bg-surface-raised"
                      >
                        {widget.title || humanizeWidgetType(widget.type)}
                      </button>
                    </li>
                  ))}
                </ul>
              );
            })()}
          </div>
        )}
      </div>
    </div>
  );
}
