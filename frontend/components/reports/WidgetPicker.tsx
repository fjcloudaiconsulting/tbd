"use client";

/**
 * Widget picker modal — appears when the user clicks "Add widget."
 *
 * v1 catalog: KPI + Bar. The button list is structured so PR3 can
 * grow it (line / area / pie / table / sparkline / stacked bar)
 * without reshaping the call site.
 */
import { BarChart3, Hash } from "lucide-react";

import type { WidgetTypeV1 } from "@/lib/reports/types";

interface Props {
  open: boolean;
  onClose: () => void;
  onPick: (type: WidgetTypeV1) => void;
}

const OPTIONS: Array<{
  type: WidgetTypeV1;
  label: string;
  description: string;
  Icon: typeof BarChart3;
}> = [
  {
    type: "kpi",
    label: "KPI",
    description: "Single number with an optional comparison.",
    Icon: Hash,
  },
  {
    type: "bar",
    label: "Bar chart",
    description: "Vertical bars over a category, account, or tag.",
    Icon: BarChart3,
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
      <div className="w-full max-w-md rounded-lg border border-border bg-surface p-5 shadow-xl">
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
        <ul className="space-y-2">
          {OPTIONS.map(({ type, label, description, Icon }) => (
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
                  <div className="text-xs text-text-muted">{description}</div>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
