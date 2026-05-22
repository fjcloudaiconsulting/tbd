"use client";

/**
 * Widget picker modal — appears when the user clicks "Add widget."
 *
 * PR3 lights up the full v1 catalog (8 types) grouped by analytical
 * intent so users land on the right widget for the question they're
 * asking:
 *
 * - Numbers     — KPI
 * - Trends      — Line, Area, Sparkline
 * - Categories  — Bar, Stacked Bar, Pie
 * - Tables      — Table
 */
import {
  AreaChart as AreaIcon,
  BarChart3,
  BarChartHorizontal,
  Hash,
  LineChart as LineIcon,
  PieChart as PieIcon,
  Table as TableIcon,
  TrendingUp,
} from "lucide-react";

import type { WidgetTypeV1 } from "@/lib/reports/types";

interface Props {
  open: boolean;
  onClose: () => void;
  onPick: (type: WidgetTypeV1) => void;
}

interface Option {
  type: WidgetTypeV1;
  label: string;
  description: string;
  Icon: typeof BarChart3;
}

interface Group {
  label: string;
  options: Option[];
}

const GROUPS: Group[] = [
  {
    label: "Numbers",
    options: [
      {
        type: "kpi",
        label: "KPI",
        description: "Single number with an optional comparison.",
        Icon: Hash,
      },
    ],
  },
  {
    label: "Trends",
    options: [
      {
        type: "line",
        label: "Line",
        description: "Time series over a date bucket; one line per series.",
        Icon: LineIcon,
      },
      {
        type: "area",
        label: "Area",
        description: "Filled time series. Stack multiple series on top.",
        Icon: AreaIcon,
      },
      {
        type: "sparkline",
        label: "Sparkline",
        description: "Compact trend line with the latest value.",
        Icon: TrendingUp,
      },
    ],
  },
  {
    label: "Categories",
    options: [
      {
        type: "bar",
        label: "Bar",
        description: "Vertical bars over a category, account, or tag.",
        Icon: BarChart3,
      },
      {
        type: "stacked_bar",
        label: "Stacked bar",
        description: "Bars with stacked sub-series per bucket.",
        Icon: BarChartHorizontal,
      },
      {
        type: "pie",
        label: "Pie",
        description: "Share-of-total with an 'Other' bucket past the top 8.",
        Icon: PieIcon,
      },
    ],
  },
  {
    label: "Tables",
    options: [
      {
        type: "table",
        label: "Table",
        description: "Sortable, paginated rows. Up to five numeric columns.",
        Icon: TableIcon,
      },
    ],
  },
];

export default function WidgetPicker({ open, onClose, onPick }: Props) {
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Add widget"
      data-testid="widget-picker"
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 px-4"
    >
      <div className="w-full max-w-2xl rounded-lg border border-border bg-surface p-5 shadow-xl">
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
          {GROUPS.map((group) => (
            <div key={group.label} data-testid={`widget-picker-group-${group.label}`}>
              <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
                {group.label}
              </div>
              <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {group.options.map(({ type, label, description, Icon }) => (
                  <li key={type}>
                    <button
                      type="button"
                      onClick={() => onPick(type)}
                      data-testid={`widget-picker-option-${type}`}
                      className="flex w-full items-start gap-3 rounded-md border border-border bg-bg p-3 text-left transition hover:border-accent hover:bg-bg-elevated"
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
          ))}
        </div>
      </div>
    </div>
  );
}
