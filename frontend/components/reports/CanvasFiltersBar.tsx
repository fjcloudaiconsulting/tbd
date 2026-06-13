"use client";

/**
 * Canvas-wide filters row — sits above the canvas. Edits flow into
 * the report's ``canvas_filters_json`` blob.
 *
 * Phase 4b: the canvas bar is DATE-ONLY. The shared date range
 * cascades to every widget that doesn't override it. Accounts,
 * categories, and tags are all per-widget now (edited in the widget
 * popover's Filters tab), so they no longer appear on the canvas.
 */
import DatePresetChips from "@/components/reports/filters/DatePresetChips";
import type { CanvasFilters } from "@/lib/reports/types";

interface Props {
  value: CanvasFilters;
  onChange: (next: CanvasFilters) => void;
}

export default function CanvasFiltersBar({ value, onChange }: Props) {
  return (
    <div
      data-testid="canvas-filters-bar"
      className="rounded-md border border-border bg-surface px-4 py-3"
    >
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
  );
}
