/**
 * Shared control constants + type guards for the widget editor.
 *
 * Extracted verbatim from the original widget config rail so the popover
 * tabs (``DataTab`` / ``StyleTab``) and the measure/filter editors all read
 * from one source. Keeping these in one module means the picker options, the
 * multi-series cap, and the single-agg lock can never drift between editors.
 */
import type { HelpTooltipKey } from "@/lib/help/tooltips";
import type {
  AreaConfig,
  Aggregation,
  Dimension,
  LineConfig,
  MeasureField,
  SourceCatalogEntry,
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

/**
 * Maps a source catalog entry's dimensions to picker options
 * (``{value: key, label}``). Used by the Data tab to drive the
 * primary/secondary dimension selects off the SELECTED source rather
 * than the static ``DIMENSION_OPTIONS`` fallback, so an accounts widget
 * never offers transactions-only dimensions (and vice versa).
 */
export function dimensionOptionsFor(
  entry: SourceCatalogEntry,
): Array<{ value: string; label: string }> {
  return entry.dimensions.map((d) => ({ value: d.key, label: d.label }));
}

/**
 * Maps a source catalog entry's measures to FIELD picker options
 * (``{value: field, label}``), de-duplicated to the distinct fields the
 * source actually publishes (e.g. transactions → amount + id; accounts →
 * balance + id), preserving catalog order. The Data tab drives the
 * measure field selects off the SELECTED source's catalog rather than the
 * static ``FIELD_OPTIONS`` fallback, so an accounts widget never offers a
 * transactions-only field like ``amount`` (and then 422s at query time).
 */
export function measureFieldOptionsFor(
  entry: SourceCatalogEntry,
): Array<{ value: string; label: string }> {
  const seen = new Set<string>();
  const out: Array<{ value: string; label: string }> = [];
  for (const m of entry.measures) {
    if (seen.has(m.field)) continue;
    seen.add(m.field);
    out.push({
      value: m.field,
      label: MEASURE_FIELD_LABELS[m.field as MeasureField] ?? m.field,
    });
  }
  return out;
}

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
