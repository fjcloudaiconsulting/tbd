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
  Measure,
  MeasureField,
  SeriesConfig,
  SourceCatalogEntry,
  StackedBarConfig,
  TableConfig,
  Widget,
} from "@/lib/reports/types";
import { DIMENSION_HEADERS, MEASURE_FIELD_LABELS } from "@/lib/reports/series";

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

// The catalog-free fallback dimension set (transactions-shaped), in editor
// order. Labels are pulled from DIMENSION_HEADERS — the same map chart axes and
// CSV headers use — so the picker fallback and the rendered output can never
// drift (they previously hard-coded separate strings for the same key).
const FALLBACK_DIMENSION_KEYS: readonly Dimension[] = [
  "category",
  "category_master",
  "account",
  "tag",
  "txn_type",
  "status",
  "month",
  "week",
  "day",
];

export const DIMENSION_OPTIONS: Array<{ value: Dimension; label: string }> =
  FALLBACK_DIMENSION_KEYS.map((value) => ({
    value,
    label: DIMENSION_HEADERS[value],
  }));

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

/**
 * The source catalog's measures as (agg, field) PAIRS, in catalog order.
 *
 * ⚠ `measureFieldOptionsFor` above de-duplicates the catalog down to distinct
 * FIELDS and throws the agg away — correct for a field picker, wrong for
 * seeding a new series. The catalog's unit of truth is the PAIR: transactions
 * publishes sum(amount), avg(amount) and count(id) over just two fields, so
 * field-only seeding cannot produce a distinct series on a single-field
 * source. That was Defect B — "+ Add series" seeded `{sum, fields[0]}`, which
 * series 1 usually already was, and the new series drew pixel-identical on
 * top of the old one.
 */
export function measurePairOptionsFor(entry: SourceCatalogEntry): Measure[] {
  return entry.measures.map((m) => ({
    agg: m.agg as Aggregation,
    field: m.field as MeasureField,
  }));
}

/** The first catalog pair not already present in ``measures``, if any. */
export function nextUnusedMeasurePair(
  pairs: Measure[] | undefined,
  measures: SeriesConfig[],
): Measure | undefined {
  if (!pairs) return undefined;
  return pairs.find(
    (p) =>
      !measures.some(
        (m) => m.measure.agg === p.agg && m.measure.field === p.field,
      ),
  );
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

// ── TBD-402: one measure select, not an agg × field cross product ───────────
//
// The editor used to offer an ``AGG_OPTIONS`` select beside a field select.
// Their cross product is not the catalog: a source publishes specific PAIRS.
// ``credit_utilization`` publishes only ``avg(utilization_pct)``, so picking
// ``sum`` beside it produced ``sum(utilization_pct)`` — a 422 on that source
// (it overrides ``validate`` with a declared-agg guard), and on sources that
// do NOT override it, something worse: ``validate_against_catalog`` checks the
// measure FIELD and never the agg (``reports/sources/base.py:80-84``), so
// ``avg(id)`` or ``count(amount)`` sails through and renders a plausible,
// meaningless number.
//
// Collapsing to one select makes an invalid pair UNREPRESENTABLE rather than
// merely validated, which is why this beats narrowing the agg list.

export interface MeasureOption {
  /** The catalog's own measure key — the select's value. */
  key: string;
  label: string;
  agg: Aggregation;
  field: MeasureField;
}

/**
 * Sentinel for a persisted measure the catalog does not publish (e.g. a
 * legacy ``distinct(id)``, or a widget whose source changed underneath it).
 *
 * ⚠ Such a pair is shown and preserved, NEVER silently rewritten. Rewriting
 * would change the number a saved report renders without telling anyone,
 * which is strictly worse than showing that it needs attention.
 */
export const UNSUPPORTED_MEASURE_KEY = "__unsupported__";

/**
 * The source catalog's measures as labelled picker options, in catalog order.
 *
 * ⚠ Keyed by the catalog's ``key``, not by ``agg:field``. Nothing guarantees
 * (agg, field) is unique across a source's measures, and the ruling in
 * ``specs/2026-08-14-tbd-381-catalog-driven-widget-editor.md`` names the key.
 */
export function measureOptionsFor(entry: SourceCatalogEntry): MeasureOption[] {
  return entry.measures.map((m) => ({
    key: m.key,
    label: m.label,
    agg: m.agg as Aggregation,
    field: m.field as MeasureField,
  }));
}

/**
 * Everything the measure select needs, in one place so the two editors
 * cannot drift.
 *
 * Three states, and they are genuinely different:
 *   - catalog still loading  → show the current measure, disabled. There is
 *     no list to choose from yet, and offering a stale fallback list is how
 *     an invalid pair got picked in the first place.
 *   - pair is in the catalog → ordinary select.
 *   - pair is NOT published  → an extra "(unsupported)" option, selected, plus
 *     a notice from the caller. Selecting a real option repairs it.
 */
export function measureSelectState(
  options: MeasureOption[] | undefined,
  measure: Measure,
  fallbackLabel: string,
): {
  options: MeasureOption[];
  value: string;
  disabled: boolean;
  unsupported: boolean;
} {
  if (options === undefined) {
    return {
      options: [
        {
          key: UNSUPPORTED_MEASURE_KEY,
          label: fallbackLabel,
          agg: measure.agg,
          field: measure.field,
        },
      ],
      value: UNSUPPORTED_MEASURE_KEY,
      disabled: true,
      unsupported: false,
    };
  }
  const match = options.find(
    (o) => o.agg === measure.agg && o.field === measure.field,
  );
  if (match) {
    return { options, value: match.key, disabled: false, unsupported: false };
  }
  return {
    options: [
      {
        key: UNSUPPORTED_MEASURE_KEY,
        label: `${fallbackLabel} (unsupported)`,
        agg: measure.agg,
        field: measure.field,
      },
      ...options,
    ],
    value: UNSUPPORTED_MEASURE_KEY,
    disabled: false,
    unsupported: true,
  };
}

/**
 * "<Agg> of <Field>" for a measure the catalog cannot label — a legacy pair,
 * or any pair while the catalog is still loading.
 *
 * Mirrors ``seriesLabel``'s fallback in ``lib/reports/series.ts``, which owns
 * the same string for chart names. That module's ``HUMAN_AGG`` is private, so
 * the agg word comes from ``AGG_OPTIONS`` — the labels are the same four
 * words, and both are here in one file rather than a third spelling.
 */
export function measureFallbackLabel(measure: Measure): string {
  const agg =
    AGG_OPTIONS.find((a) => a.value === measure.agg)?.label ?? measure.agg;
  const field =
    MEASURE_FIELD_LABELS[measure.field as MeasureField] ?? measure.field;
  return `${agg} of ${field}`;
}

/**
 * The one sentence shown when a persisted measure is not in the catalog.
 *
 * ⚠ Shared by both editors deliberately — same precedent as
 * ``lib/demotion.ts``: a user meeting this on a KPI and on a line series
 * must not be told two different things about the same condition.
 */
export const UNSUPPORTED_MEASURE_NOTICE =
  "This measure is not offered by the selected data source. It has been left as-is; pick one from the list to change it.";
