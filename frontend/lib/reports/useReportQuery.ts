/**
 * SWR-backed query hook for Reports v2 widgets.
 *
 * The widget owns its own SWR cache entry keyed by widget id +
 * serialized resolved query. Canvas filter change → widget re-renders
 * with a new resolved query → SWR fires a new fetch. Failure of one
 * widget shows an inline error inside that widget and does not block
 * the others (per spec §7 "Frontend fetching").
 */
import useSWR from "swr";
import { useMemo } from "react";

import { runQuery } from "./api";
import { astLimitForBarFamily } from "./breakdown";
import {
  resolveFilters,
  sourceSupportsDateFilter,
  sourceSupportsStatusFilter,
} from "./resolve";
import { useReportSources } from "./use-report-sources";
import type {
  CanvasFilters,
  Filter,
  KPIWidget,
  Measure,
  QueryMeta,
  ReportsQuery,
  ReportsQueryResponse,
  Widget,
  WidgetFilters,
} from "./types";

interface UseReportQueryResult {
  data: ReportsQueryResponse | undefined;
  error: Error | undefined;
  isLoading: boolean;
  /** The AST that produced this result; useful for tests + debugging. */
  query: ReportsQuery;
}

/**
 * Builds the AST for a widget and runs it through SWR. The cache key
 * is ``["report-query", widgetId, JSON.stringify(query)]`` so two
 * widgets with identical configs share the same fetch (and two
 * different configs do not).
 */
export function useReportQuery(
  widget: Widget,
  canvasFilters: CanvasFilters | undefined,
): UseReportQueryResult {
  const { sources } = useReportSources();
  const supportsDate = sourceSupportsDateFilter(sources, widget.config.dataset);
  const supportsStatus = sourceSupportsStatusFilter(
    sources,
    widget.config.dataset,
  );
  const query = useMemo<ReportsQuery>(() => {
    return buildQueryAst(widget, canvasFilters, supportsDate, supportsStatus);
  }, [widget, canvasFilters, supportsDate, supportsStatus]);

  const swrKey = ["report-query", widget.id, JSON.stringify(query)];
  const { data, error, isLoading } = useSWR<ReportsQueryResponse>(
    swrKey,
    () => runQuery(query),
    {
      revalidateOnFocus: false,
      revalidateIfStale: true,
      shouldRetryOnError: false,
    },
  );

  return { data, error, isLoading, query };
}

/**
 * Reads the scalar a one-row aggregate query returns.
 *
 * Shared by the KPI's value and its comparison value so the two can never
 * coerce differently (a string `"0"` that reads as a number on one side and
 * as `null` on the other would silently suppress or invent a delta).
 */
export function readMeasureValue(
  row: Record<string, string | number | null> | undefined,
): number | null {
  if (!row) return null;
  const v = row.value;
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Why a prior-period window could not be computed (TBD-383).
 *
 * ⚠ The reason is part of the contract, not debug colour. Every refusal looks
 * identical from outside the widget — the value renders with no delta and no
 * second query fires — so without a named reason a fence cannot tell a
 * deliberate refusal from an implementation that read an absent `start`/`end`,
 * got `undefined`, and was right by accident. `tests/lib/reports/prior-window.test.ts`
 * asserts each one.
 *
 * ⚠⚠ NOTHING IN THE UI CONSUMES THIS YET, AND THAT IS DELIBERATE — DO NOT
 * DELETE IT AS DEAD CODE. Surfacing "why there is no delta" to the user is a
 * new user-facing surface and is tracked separately. This taxonomy exists so
 * the resolver's tests can distinguish a deliberate refusal from an accidental
 * one, and it is the ONLY fence that makes the `relative_token` refusal
 * deliberate: delete it and every component-level fence stays green while the
 * `next_cycle` guard silently becomes "right by accident". The same applies to
 * `error` and `isLoading` on `UseComparisonQueryResult`.
 */
export type PriorWindowRefusal =
  | "no_date_filter"
  | "half_open"
  | "relative_token"
  | "malformed"
  | "inverted"
  | "future_window";

export type PriorWindowResult =
  | { prior: [string, string]; refusal: null }
  | { prior: null; refusal: PriorWindowRefusal };

const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const MS_PER_DAY = 86_400_000;

/** `YYYY-MM-DD` → whole days since the epoch, or null if it is not a real date. */
function toEpochDay(iso: unknown): number | null {
  if (typeof iso !== "string") return null;
  const m = ISO_DAY.exec(iso);
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const ms = Date.UTC(y, mo - 1, d);
  if (!Number.isFinite(ms)) return null;
  const back = new Date(ms);
  // ⚠ `Date.UTC` rolls over silently: 2026-02-30 becomes 2026-03-02. Accepting
  // that would shift the comparison window by two days with no symptom.
  if (
    back.getUTCFullYear() !== y ||
    back.getUTCMonth() !== mo - 1 ||
    back.getUTCDate() !== d
  ) {
    return null;
  }
  return Math.round(ms / MS_PER_DAY);
}

function fromEpochDay(day: number): string {
  return new Date(day * MS_PER_DAY).toISOString().slice(0, 10);
}

/**
 * The window immediately before the resolved one, of the same REACHED length:
 *
 *     effective.end = min(window.end, today)
 *     prior.end     = window.start - 1 day
 *     prior.start   = prior.end - (effective.length - 1 day)
 *
 * ⚠⚠ THE CLAMP TO `today` IS LOAD-BEARING, NOT A REFINEMENT (TBD-383 B1).
 * `draft.ts` seeds every new report with the `this_month` preset, and
 * `buildPresetRanges` freezes that as the WHOLE calendar month
 * (`startOfMonth(now)..endOfMonth(now)`), not month-to-date. Unclamped, on
 * 2026-09-02 that compared 2 days of September against 30 complete days of
 * August and rendered roughly "-92% vs prior period" for a user whose
 * spending had not changed — about -100% on the 1st of any month. That is the
 * default authoring path, for most of every month, under a label asserting
 * comparability: absent would have become actively FALSE.
 *
 * ⚠ Only `this_month` breaks THE CLAMP. `last_month` needs no clamping (it is
 * fully past) and `ytd` / `last_12_months` end at `now`, so manual testing on
 * any other preset looks perfect. Do not "simplify" the clamp away on the
 * evidence of one preset. ⚠ "`last_month` is correct" is a statement about the
 * clamp ONLY — it is still subject to the sliding-window question below, where
 * a 28-day February maps to 2026-01-04..01-31 and drops January 1-3.
 *
 * ⚠ Refusing a future-ending window instead was rejected: it kills the delta
 * on the default preset entirely, which is worse than a clamped one.
 *
 * ⚠ THIS IS NOT COMPLETE-TO-COMPLETE, AND THE COMMENT MUST NOT CLAIM IT IS.
 * `today` counts as a whole day on both sides, so on day N you compare N-1
 * complete days PLUS a partial today against N complete days. Unchanged
 * spending still reads about -29% on day 2 at 10:00 and about -4% by day 15 —
 * far better than the -92% this replaced, but not zero. `min(end, today - 1)`
 * would be genuinely complete-to-complete; it is NOT used because it refuses
 * on the 1st of the month, when there is no complete day yet.
 *
 * ⚠ `today` is a REQUIRED PARAMETER. This resolver must stay wall-clock-free
 * so its tests are not date bombs; the caller supplies the day.
 *
 * ⚠ It reads the RESOLVED `date` filter, never the persisted `date_range`.
 * The shift is an operation on the window the backend will actually see; the
 * config may hold an inherited canvas value, a widget override, or a token.
 *
 * Refuses, deliberately, for:
 *   - no `date` filter at all — the query is unbounded, so there is no
 *     "prior" anything;
 *   - `gte` / `lte` — half-open, no length to shift by;
 *   - ⚠⚠ `op: "relative"` (today: `preset: "next_cycle"`) — the client holds
 *     ONLY a token. The absolute window is resolved server-side, per request,
 *     against the org's billing cycle. The client therefore cannot compute the
 *     prior window and must not guess one. This arm is why the refusal is
 *     matched on the resolved `op` rather than on the presence of
 *     `start`/`end`: reading `start` would refuse here too, but only by
 *     accident, and would start returning a wrong window the moment a relative
 *     token gains client-visible bounds.
 *
 * A KPI with no delta is the ordinary state, not an error.
 */
export function resolvePriorWindow(
  filters: Filter[],
  today: string,
): PriorWindowResult {
  const date = filters.find((f) => f.field === "date");
  if (!date) return { prior: null, refusal: "no_date_filter" };
  if (date.op === "relative") return { prior: null, refusal: "relative_token" };
  if (date.op === "gte" || date.op === "lte") {
    return { prior: null, refusal: "half_open" };
  }
  if (date.op !== "between" || !Array.isArray(date.value) || date.value.length !== 2) {
    return { prior: null, refusal: "malformed" };
  }
  const start = toEpochDay(date.value[0]);
  const end = toEpochDay(date.value[1]);
  const now = toEpochDay(today);
  if (start === null || end === null || now === null) {
    return { prior: null, refusal: "malformed" };
  }
  if (end < start) return { prior: null, refusal: "inverted" };
  // Only the elapsed part of the window holds data, so only the elapsed part
  // may set the comparison length.
  const effectiveEnd = Math.min(end, now);
  if (effectiveEnd < start) return { prior: null, refusal: "future_window" };
  const lengthDays = effectiveEnd - start + 1;
  return {
    prior: [fromEpochDay(start - lengthDays), fromEpochDay(start - 1)],
    refusal: null,
  };
}

/**
 * Today's calendar date as `YYYY-MM-DD`, from the LOCAL clock.
 *
 * ⚠ Local, not `toISOString()`. The windows this is compared against are built
 * by `date-presets.ts` from local-clock components, so a UTC day index would
 * be off by one either side of midnight in a non-zero offset and would clamp
 * `this_month` to the wrong number of days.
 *
 * ⚠ FOR THE RECORD, NOT A BUG TO FIX HERE: a second skew exists on the server
 * side. `backend/app/reports/templates.py` computes its `this_month` /
 * `last_12_months` windows from Python's `date.today()` — the SERVER clock —
 * and those windows persist into `canvas_filters_json` when a report is created
 * from a template. A client an hour behind the server can therefore meet a
 * window that starts "tomorrow". It fails SAFE: `effectiveEnd < start` yields
 * the `future_window` refusal, so the KPI renders its value with no delta and
 * self-heals within a day. Do not "fix" it by loosening the refusal.
 */
function todayIso(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

interface UseComparisonQueryResult {
  /** The prior window's value, or null when there is no comparison to show. */
  prior: number | null;
  isLoading: boolean;
  error: Error | undefined;
  /** Null when a comparison ran (or was never asked for); see `PriorWindowRefusal`. */
  refusal: PriorWindowRefusal | null;
  /** The AST that produced `prior`; null when no query was issued. */
  query: ReportsQuery | null;
}
/**
 * The KPI's prior-period companion query (TBD-383).
 *
 * ⚠⚠ THIS IS DELIBERATELY A HOOK THE WIDGET CALLS, NOT A PROP A CALLER PASSES.
 * `compare_prior_period` shipped as a `priorValue` prop that no production
 * caller ever supplied, so the checkbox wrote a flag nothing read and the
 * delta never rendered — for the life of the widget, with a green test the
 * whole time, because the tests injected the prop. A render branch fed from
 * outside can be forgotten by a caller; one fed from the widget's own config
 * cannot. Do not reintroduce an injected prior value.
 *
 * It reuses `buildQueryAst` verbatim and swaps only the resolved `date`
 * filter, so the comparison is over the same dataset, measure, and population
 * as the number it sits under.
 *
 * Fires NO request at all when the widget did not ask for a comparison, or
 * when the window is not shiftable — SWR is handed a null key. The 95% path
 * gains no network call.
 */
export function useComparisonQuery(
  widget: KPIWidget,
  canvasFilters: CanvasFilters | undefined,
): UseComparisonQueryResult {
  const { sources } = useReportSources();
  const supportsDate = sourceSupportsDateFilter(sources, widget.config.dataset);
  const supportsStatus = sourceSupportsStatusFilter(
    sources,
    widget.config.dataset,
  );
  const enabled = widget.config.compare_prior_period === true;
  // Recomputed each render (a string, so it is stable by value and does not
  // thrash the memo) and listed in the deps, so a session that crosses
  // midnight re-derives the comparison window instead of holding yesterday's.
  const today = todayIso();

  const { query, refusal } = useMemo<{
    query: ReportsQuery | null;
    refusal: PriorWindowRefusal | null;
  }>(() => {
    if (!enabled) return { query: null, refusal: null };
    const base = buildQueryAst(widget, canvasFilters, supportsDate, supportsStatus);
    const resolved = resolvePriorWindow(base.filters, today);
    if (resolved.prior === null) {
      return { query: null, refusal: resolved.refusal };
    }
    const window: [string, string] = resolved.prior;
    return {
      query: {
        ...base,
        filters: base.filters.map((f) =>
          f.field === "date"
            ? { field: "date" as const, op: "between" as const, value: [...window] }
            : f,
        ),
      },
      refusal: null,
    };
  }, [enabled, widget, canvasFilters, supportsDate, supportsStatus, today]);

  const swrKey = query
    ? ["report-query-prior", widget.id, JSON.stringify(query)]
    : null;
  const { data, error, isLoading } = useSWR<ReportsQueryResponse>(
    swrKey,
    () => runQuery(query as ReportsQuery),
    {
      revalidateOnFocus: false,
      revalidateIfStale: true,
      shouldRetryOnError: false,
    },
  );

  return {
    prior: readMeasureValue(data?.rows[0]),
    isLoading: !!isLoading,
    error: (error as Error | undefined) ?? undefined,
    refusal,
    query,
  };
}

/**
 * Pure builder so tests + the editor's save handler can construct the
 * exact AST a widget would query, without going through SWR.
 */
export function buildQueryAst(
  widget: Widget,
  canvasFilters: CanvasFilters | undefined,
  sourceSupportsDate = true,
  sourceSupportsStatus = true,
): ReportsQuery {
  const widgetFilters: WidgetFilters | undefined =
    "filters" in widget.config ? widget.config.filters : undefined;
  const filters = resolveFilters(
    canvasFilters,
    widgetFilters,
    sourceSupportsDate,
    sourceSupportsStatus,
  );
  // Opt-in "raw activity" flag — transactions-only. On any other source the
  // backend ignores it, but we drop it here too so the AST (and its SWR
  // cache key) stays clean and a stale persisted value can't leak across a
  // source switch.
  const include_non_reportable =
    widget.config.dataset === "transactions" &&
    !!widgetFilters?.include_non_reportable;

  if (widget.type === "kpi") {
    return {
      dataset: widget.config.dataset,
      measure: widget.config.measure,
      dimensions: [],
      filters,
      limit: 1,
      include_non_reportable,
    };
  }

  // The bar family. TBD-382 folded ``stacked_bar`` in here: it lost its
  // measure-stacking axis, so like ``bar`` it is ONE query over
  // ``[primary, secondary]`` pivoted client-side. The two types still persist
  // their measure under different keys (``measure`` vs a length-1
  // ``measures``), which is why the adapter lives here rather than in a
  // shared shape.
  //
  // ⚠ The single-dimension default differs per type and must stay that way:
  // ``bar`` has always defaulted to 10 buckets, ``stacked_bar`` to 100 as a
  // multi-series widget. Collapsing them silently cuts a 1-dimension stacked
  // bar to ten rows.
  if (widget.type === "bar" || widget.type === "stacked_bar") {
    const measure: Measure =
      widget.type === "bar"
        ? widget.config.measure
        : (widget.config.measures[0]?.measure ?? { agg: "sum", field: "amount" });
    return {
      dataset: widget.config.dataset,
      measure,
      dimensions: widget.config.dimensions,
      filters,
      sort: widget.config.sort,
      limit: astLimitForBarFamily(
        widget.config.dimensions,
        widget.config.limit,
        widget.type === "bar" ? 10 : 100,
      ),
      include_non_reportable,
    };
  }

  if (
    widget.type === "line" ||
    widget.type === "area" ||
    widget.type === "table"
  ) {
    // Multi-series widgets: ``buildQueryAst`` returns the FIRST series'
    // AST as a convenience for callers that just need the shared
    // dimension/filter shape; widgets that render >1 series compose
    // multiple queries via ``buildSeriesQueryAst``.
    // ⚠ ``stacked_bar`` left this branch in TBD-382 — it is single-measure
    // now, so there is nothing to fan out.
    const firstMeasure: Measure =
      widget.config.measures[0]?.measure ?? { agg: "sum", field: "amount" };
    return {
      dataset: widget.config.dataset,
      measure: firstMeasure,
      dimensions: widget.config.dimensions,
      filters,
      sort: widget.config.sort,
      limit: widget.config.limit ?? 100,
      include_non_reportable,
    };
  }

  if (widget.type === "pie") {
    return {
      dataset: widget.config.dataset,
      measure: widget.config.measure,
      dimensions: widget.config.dimensions,
      filters,
      sort: widget.config.sort,
      limit: widget.config.limit ?? 50,
      include_non_reportable,
    };
  }

  // Sankey widgets use useSankeyQuery, not useReportQuery. This branch
  // is unreachable in normal operation; it guards the type-checker so
  // the sparkline fallback below doesn't try to read SankeyConfig fields
  // (dimensions/sort/limit) that don't exist on SankeyConfig.
  if (widget.type === "sankey") {
    throw new Error(
      "buildQueryAst called for a sankey widget; use useSankeyQuery instead",
    );
  }

  // Sparkline.
  return {
    dataset: widget.config.dataset,
    measure: widget.config.measure,
    dimensions: widget.config.dimensions,
    filters,
    sort: widget.config.sort,
    limit: widget.config.limit ?? 50,
    include_non_reportable,
  };
}

/**
 * Per-series AST builder. Reuses the widget's resolved filters and
 * dimension list, swapping the ``measure`` in for the specific series.
 * Used by multi-series widgets (line / area / table)
 * that fire one query per measure and merge the rows client-side by
 * the first dimension key.
 */
export function buildSeriesQueryAst(
  widget: Widget,
  measure: Measure,
  canvasFilters: CanvasFilters | undefined,
  sourceSupportsDate = true,
  sourceSupportsStatus = true,
): ReportsQuery {
  const widgetFilters: WidgetFilters | undefined =
    "filters" in widget.config ? widget.config.filters : undefined;
  const filters = resolveFilters(
    canvasFilters,
    widgetFilters,
    sourceSupportsDate,
    sourceSupportsStatus,
  );
  const dimensions =
    "dimensions" in widget.config ? widget.config.dimensions : [];
  const sort = "sort" in widget.config ? widget.config.sort : undefined;
  const limit = "limit" in widget.config ? widget.config.limit : undefined;
  const include_non_reportable =
    widget.config.dataset === "transactions" &&
    !!widgetFilters?.include_non_reportable;
  return {
    dataset: widget.config.dataset,
    measure,
    dimensions,
    filters,
    sort,
    limit: limit ?? 100,
    include_non_reportable,
  };
}

/**
 * Multi-series query hook. Returns one ``data`` entry per series in
 * the widget config, plus a combined loading / error state.
 *
 * Implementation note: React's rules of hooks forbid calling hooks
 * inside a variable-length loop. Instead of pre-allocating N SWR
 * hooks, we use ONE ``useSWR`` whose fetcher fires the per-series
 * queries in parallel via ``Promise.all``. The cache key includes a
 * serialized list of all per-series ASTs, so editing any series
 * invalidates the combined fetch.
 */
export function useSeriesQueries(
  widget: Widget,
  canvasFilters: CanvasFilters | undefined,
  measures: Measure[],
): {
  series: Array<ReportsQueryResponse | undefined>;
  /**
   * Per-series ``meta``, index-aligned with ``series``.
   *
   * ⚠ TBD-430: this hook used to discard ``meta`` entirely, which made
   * ``truncated`` and ``warning`` structurally invisible to line, area,
   * table and the multi-measure stacked bar — half the widget catalog.
   * Anything deriving a notice must read EVERY entry, not ``[0]``: a
   * two-measure table whose SECOND query hit the cap renders an
   * incomplete merge just as surely as one whose first did.
   */
  metas: Array<QueryMeta | undefined>;
  isLoading: boolean;
  error: Error | undefined;
} {
  const { sources } = useReportSources();
  const supportsDate = sourceSupportsDateFilter(sources, widget.config.dataset);
  const supportsStatus = sourceSupportsStatusFilter(
    sources,
    widget.config.dataset,
  );
  const queries = useMemo(
    () =>
      measures.map((m) =>
        buildSeriesQueryAst(
          widget,
          m,
          canvasFilters,
          supportsDate,
          supportsStatus,
        ),
      ),
    [widget, canvasFilters, measures, supportsDate, supportsStatus],
  );
  const swrKey = ["report-series-query", widget.id, JSON.stringify(queries)];
  const { data, error, isLoading } = useSWR<ReportsQueryResponse[]>(
    swrKey,
    () => Promise.all(queries.map((q) => runQuery(q))),
    {
      revalidateOnFocus: false,
      revalidateIfStale: true,
      shouldRetryOnError: false,
    },
  );

  const series = useMemo(
    () => data ?? measures.map(() => undefined),
    [data, measures],
  );
  const metas = useMemo(() => series.map((r) => r?.meta), [series]);

  return {
    series,
    metas,
    isLoading: !!isLoading,
    error: (error as Error | undefined) ?? undefined,
  };
}
