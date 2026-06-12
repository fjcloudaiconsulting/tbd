/**
 * Shared control constants + type guards for the widget editor.
 *
 * Extracted verbatim from ``ConfigRail.tsx`` so the popover tabs
 * (``DataTab`` / ``StyleTab``), the measure/filter editors, and (in the
 * extraction PR) ``ConfigRail`` itself all read from one source. Keeping
 * these in one module means the picker options, the multi-series cap, and
 * the single-agg lock can never drift between the rail and the popover.
 */
import type { HelpTooltipKey } from "@/lib/help/tooltips";
import type {
  AreaConfig,
  Aggregation,
  Dimension,
  LineConfig,
  MeasureField,
  StackedBarConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";
import { MEASURE_FIELD_LABELS } from "@/lib/reports/series";

export const AGG_OPTIONS: Array<{ value: Aggregation; label: string }> = [
  { value: "sum", label: "Sum" },
  { value: "count", label: "Count" },
  { value: "avg", label: "Average" },
  { value: "distinct", label: "Distinct count" },
];

/** Tooltip key for each aggregation type (plain-language explainer). */
export const AGG_HELP_KEY: Record<Aggregation, HelpTooltipKey> = {
  sum: "reports.agg.sum",
  count: "reports.agg.count",
  avg: "reports.agg.avg",
  distinct: "reports.agg.distinct",
};

// Derived from the shared measure-field label map so the editor picker,
// chart tooltips, and CSV headers can never drift apart.
export const FIELD_OPTIONS: Array<{ value: MeasureField; label: string }> = (
  Object.keys(MEASURE_FIELD_LABELS) as MeasureField[]
).map((value) => ({ value, label: MEASURE_FIELD_LABELS[value] }));

export const DIMENSION_OPTIONS: Array<{ value: Dimension; label: string }> = [
  { value: "category", label: "Category" },
  { value: "category_master", label: "Master category" },
  { value: "account", label: "Account" },
  { value: "tag", label: "Tag" },
  { value: "txn_type", label: "Transaction type" },
  { value: "status", label: "Status" },
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
];

export const MAX_SERIES = 5;
export const MAX_TABLE_COLUMNS = 5;

/** Widget types that carry ``config.measures`` (multi-series). */
export function isMultiSeries(
  w: Widget,
): w is Widget & { config: LineConfig | AreaConfig | StackedBarConfig | TableConfig } {
  return (
    w.type === "line" ||
    w.type === "area" ||
    w.type === "stacked_bar" ||
    w.type === "table"
  );
}

/** Widget types locked to a single dimension + single aggregation. */
export function isSingleAggLocked(w: Widget): boolean {
  return w.type === "pie" || w.type === "sparkline";
}
