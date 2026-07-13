"use client";

/**
 * Canvas-wide filters row — sits above the canvas. Edits flow into
 * the report's ``canvas_filters_json`` blob.
 *
 * Phase 4b: the canvas bar carried a DATE-ONLY control. Feature 1 adds a
 * shared Settled/Pending STATUS control below it; both cascade to every
 * transactions widget that doesn't override them. Accounts, categories,
 * and tags remain per-widget (edited in the widget popover's Filters
 * tab), so they still don't appear on the canvas.
 *
 * ``hideDate`` renders the status control alone — used by the dashboard,
 * which already owns its period via ``DashboardPeriodNav`` and would
 * collide with a second date control.
 */
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import StatusFilter from "@/components/reports/filters/StatusFilter";
import type { CanvasFilters } from "@/lib/reports/types";

interface Props {
  value: CanvasFilters;
  onChange: (next: CanvasFilters) => void;
  /**
   * When true, the shared date control is hidden and only the status
   * control renders. The dashboard sets this so its status control never
   * collides with ``DashboardPeriodNav``.
   */
  hideDate?: boolean;
}

export default function CanvasFiltersBar({
  value,
  onChange,
  hideDate = false,
}: Props) {
  return (
    <div
      data-testid="canvas-filters-bar"
      className="flex flex-col gap-3 rounded-md border border-border bg-surface px-4 py-3"
    >
      {!hideDate && (
        <div>
          <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-text-muted">
            Date range
          </span>
          <DatePresetChips
            value={value.date_range}
            ariaPrefix="Canvas"
            onChange={(next) =>
              onChange({ ...value, date_range: next || undefined })
            }
          />
        </div>
      )}
      <div>
        <span className="mb-1 block text-[10px] font-medium uppercase tracking-wider text-text-muted">
          Status
        </span>
        <StatusFilter
          value={value.status}
          label=""
          ariaPrefix="Canvas status"
          onChange={(status) => onChange({ ...value, status })}
        />
      </div>
    </div>
  );
}
