/**
 * Reports v2 — blank draft seed.
 *
 * A truly empty report renders nothing (canvas filters cascade INTO
 * widgets, and there are no widgets), so a fresh draft seeds one
 * sensible starter widget plus a this-month canvas date so the draft
 * shows data immediately. This is the content the list page's "New
 * report" button used to persist directly; it now lives only in the
 * unsaved draft until the user Saves.
 */
import { buildPresetRanges } from "@/components/reports/filters/DatePresetChips";
import type { CanvasFilters, LayoutJson } from "@/lib/reports/types";

export interface DraftSeed {
  layout: LayoutJson;
  canvasFilters: CanvasFilters;
}

export function blankDraftSeed(now: Date = new Date()): DraftSeed {
  const ranges = buildPresetRanges(now);
  return {
    canvasFilters: { date_range: ranges.this_month },
    layout: {
      version: 1,
      widgets: [
        {
          id: "w_start",
          type: "bar",
          title: "Spend by category",
          grid: { x: 0, y: 0, w: 6, h: 4 },
          config: {
            dataset: "transactions",
            measure: { agg: "sum", field: "amount" },
            dimensions: ["category"],
            filters: { txn_type: ["expense"] },
            sort: { by: "value", dir: "desc" },
            limit: 10,
            format: "currency",
          },
        },
      ],
    },
  };
}
