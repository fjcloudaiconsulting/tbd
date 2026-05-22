"use client";

/**
 * Canvas-wide filters row — sits above the canvas. Edits flow into
 * the report's ``canvas_filters_json`` blob and cascade to every
 * widget that doesn't override the same field (spec section 4).
 *
 * PR3 swaps the comma-list inputs for the proper filter primitives:
 *  - Date range  -> ``DatePresetChips`` (preset chips + custom
 *    absolute inputs).
 *  - Accounts    -> ``AccountFilter`` (chip picker, fetches the org's
 *    accounts on mount).
 *  - Categories  -> ``CategoryPicker`` (tree picker with master /
 *    sub cascade, multi-select).
 *
 * Tag-based filters DO NOT appear on the canvas — they're a
 * per-widget knob per spec section 4 ("the canvas filter shape is
 * date range + accounts + categories; tag filters are widget-only").
 */
import AccountFilter from "@/components/reports/filters/AccountFilter";
import CategoryPicker from "@/components/reports/filters/CategoryPicker";
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
      className="grid grid-cols-1 gap-4 rounded-md border border-border bg-surface px-4 py-3 lg:grid-cols-3"
    >
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
      <AccountFilter
        value={value.account_ids ?? []}
        ariaPrefix="Canvas account"
        onChange={(account_ids) =>
          onChange({
            ...value,
            account_ids: account_ids.length > 0 ? account_ids : undefined,
          })
        }
      />
      <CategoryPicker
        value={value.category_ids ?? []}
        onChange={(category_ids) =>
          onChange({
            ...value,
            category_ids: category_ids.length > 0 ? category_ids : undefined,
          })
        }
      />
    </div>
  );
}
