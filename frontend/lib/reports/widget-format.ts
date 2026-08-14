/**
 * Derive a widget's number format from the source catalog, at RENDER time.
 *
 * TBD-381. This replaces `config.format`, which was persisted state that went
 * stale. Format is a pure function of (measure, catalog) and is now derived
 * every render instead of cached at write time.
 *
 * ## Why derivation moved here
 *
 * `GET /api/v1/reports/sources` publishes a `format` per measure row. Deriving
 * at MUTATION time meant remembering to call a resolver at every site a widget
 * config is born or edited. That set is 28, across five files and two
 * languages, and 14 of them are in `backend/app/reports/templates.py` where no
 * frontend resolver can reach:
 *
 *   config/useWidgetMutations.ts   3     widgetKit.tsx                5
 *   app/reports/[id]/page.tsx      5     lib/reports/draft.ts         1
 *   backend/app/reports/templates.py                                 14
 *
 * The read side is eight lines. It was already broken in shipped code, which
 * is what settles it: `widgetKit.tsx` seeded `format: "number"` for a
 * `sum(amount)` sparkline (every new sparkline rendered raw numbers), and the
 * `cdd-pie-share` template omitted `format` entirely. Neither goes through a
 * mutation, so neither was reachable by a mutation-time fix.
 *
 * ⚠ NOT in `formatMeasureValue` (`series.ts`). That is a pure function taking
 * `format` as a parameter, called from nine chart files; the catalog is not in
 * scope there and making it so would mean turning it into a hook. The ticket
 * named it as the render site and was wrong.
 *
 * ## Why reading the catalog here is safe
 *
 * `/reports/sources` sits behind the SAME `require_feature(Feature.REPORTS)`
 * gate as `/reports/query` (`routers/reports.py`), so AUTHORIZATION-driven
 * unavailability coincides -- including on the dashboard tile path,
 * where `renderDashboardWidget` delegates to `renderReportWidget` and mounts
 * these same components. No new failure mode. `useReportSources` is a
 * constant-key SWR hook, so this deduplicates against the call already inside
 * `useReportQuery` / `useSeriesQueries` rather than adding a request.
 *
 * ⚠ That argument covers the feature gate and auth, NOT transport. They are
 * separate SWR keys with independent lifecycles, so `/query` succeeding while
 * `/sources` 500s or times out is ordinary. `useReportSources` swallows SWR's
 * error and returns `{sources: [], isLoading: false}`, so in that state the
 * skeleton releases and every widget renders `"number"` -- a currency value
 * loses its symbol and a percent loses its `%`. That is a real regression
 * against the old persisted format, which was at least right for currency
 * widgets. Surfacing the error so callers can degrade visibly is filed as a
 * follow-up rather than smuggled in here.
 */
import type { Measure, SourceCatalogEntry } from "@/lib/reports/types";
import { useReportSources } from "@/lib/reports/use-report-sources";

/** The format vocabulary the widget configs and `formatMeasureValue` share. */
export type WidgetFormat = "currency" | "number" | "percent";

const DEFAULT_FORMAT: WidgetFormat = "number";

/**
 * The catalog's `format` is a bare string on the wire. The deleted
 * mutation-time resolver validated it; this one must too, or a source
 * publishing a fourth value some day (`"duration"`, `"count"`) flows straight
 * into `formatMeasureValue`, matches no branch, and silently falls through to
 * `toLocaleString()`.
 */
function asFormat(value: string | undefined): WidgetFormat | undefined {
  return value === "currency" || value === "number" || value === "percent"
    ? value
    : undefined;
}

/**
 * Resolve one measure's format against a catalog entry.
 *
 * ⚠ Matches the exact (agg, field) PAIR first. The old mutation-time resolver
 * carried a load-bearing comment saying "MATCH ON FIELD ONLY, do NOT add
 * `&& m.agg === measure.agg`" -- that guarded a mutation-time hazard, namely
 * preserving a stale previous format when the lookup missed. At render there
 * is no previous value to preserve, so the exact match is not only safe but
 * strictly better: it is what makes a legacy `count(amount)` render as a
 * cardinality rather than as currency.
 *
 * Step 3 is a backstop, not a guess: verified across all five sources, no
 * field currently maps to two different formats.
 */
export function formatForMeasure(
  entry: SourceCatalogEntry | undefined,
  measure: Measure | undefined,
): WidgetFormat | undefined {
  if (!entry || !measure) return undefined;

  // 1. exact (agg, field)
  const exact = entry.measures.find(
    (m) => m.field === measure.field && m.agg === measure.agg,
  );
  if (exact) return asFormat(exact.format) ?? DEFAULT_FORMAT;

  // 2. a cardinality is never currency, whatever the underlying field is
  if (measure.agg === "count" || measure.agg === "distinct") return "number";

  // 3. field-only backstop
  const byField = entry.measures.find((m) => m.field === measure.field);
  if (byField) return asFormat(byField.format) ?? DEFAULT_FORMAT;

  // 4. unknown measure (legacy config, or a source that dropped a measure)
  return DEFAULT_FORMAT;
}

/**
 * The format for a set of measures sharing ONE axis.
 *
 * Shared-axis charts (kpi, bar, line, area, stacked_bar, pie, sparkline) pass a
 * single format into a single Recharts `tickFormatter` for one `<YAxis>`. With
 * series of differing formats there is no honest single answer, so we fall to
 * `"number"` rather than stamping series[0]'s unit on a shared scale -- that
 * would not merely under-serve the other series, it would MISLABEL them.
 *
 * `TableWidget` is the exception and does NOT use this: it formats cell by
 * cell, so per-column derivation is coherent there. Use `formatForMeasure` per
 * column instead.
 */
export function sharedFormatFor(
  entry: SourceCatalogEntry | undefined,
  measures: Array<Measure | undefined>,
): WidgetFormat | undefined {
  if (!entry) return undefined;
  const resolved = measures
    .map((m) => formatForMeasure(entry, m))
    .filter((f): f is WidgetFormat => f !== undefined);
  if (resolved.length === 0) return undefined;
  const [first] = resolved;
  return resolved.every((f) => f === first) ? first : DEFAULT_FORMAT;
}

/** Look a dataset up in the catalog. */
export function entryFor(
  sources: SourceCatalogEntry[],
  dataset: string | undefined,
): SourceCatalogEntry | undefined {
  if (!dataset) return undefined;
  return sources.find((s) => s.key === dataset);
}

/**
 * Render-time format for a widget, from its own dataset + measures.
 *
 * Returns `undefined` while the catalog is loading. Callers hold their existing
 * loading skeleton in that window rather than rendering an unformatted value --
 * `/query` is in flight over the same window anyway, so this costs nothing on
 * first paint and avoids showing a wrong unit that then flips.
 */
export function useWidgetFormat(
  dataset: string | undefined,
  measures: Array<Measure | undefined>,
): { format: WidgetFormat | undefined; entry: SourceCatalogEntry | undefined; isLoading: boolean } {
  const { sources, isLoading } = useReportSources();
  const entry = entryFor(sources, dataset);
  return { format: sharedFormatFor(entry, measures), entry, isLoading };
}
