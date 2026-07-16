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
import {
  resolveFilters,
  sourceSupportsDateFilter,
  sourceSupportsStatusFilter,
} from "./resolve";
import { useReportSources } from "./use-report-sources";
import type {
  CanvasFilters,
  Measure,
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

  if (widget.type === "bar") {
    return {
      dataset: widget.config.dataset,
      measure: widget.config.measure,
      dimensions: widget.config.dimensions,
      filters,
      sort: widget.config.sort,
      limit: widget.config.limit ?? 10,
      include_non_reportable,
    };
  }

  if (
    widget.type === "line" ||
    widget.type === "area" ||
    widget.type === "stacked_bar" ||
    widget.type === "table"
  ) {
    // Multi-series widgets: ``buildQueryAst`` returns the FIRST series'
    // AST as a convenience for callers that just need the shared
    // dimension/filter shape; widgets that render >1 series compose
    // multiple queries via ``buildSeriesQueryAst``.
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
 * Used by multi-series widgets (line / area / stacked bar / table)
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

  return {
    series: data ?? measures.map(() => undefined),
    isLoading: !!isLoading,
    error: (error as Error | undefined) ?? undefined,
  };
}
