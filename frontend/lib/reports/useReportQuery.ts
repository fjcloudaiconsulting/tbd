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
import { resolveFilters } from "./resolve";
import type {
  CanvasFilters,
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
  const query = useMemo<ReportsQuery>(() => {
    return buildQueryAst(widget, canvasFilters);
  }, [widget, canvasFilters]);

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
): ReportsQuery {
  const widgetFilters: WidgetFilters | undefined =
    "filters" in widget.config ? widget.config.filters : undefined;
  const filters = resolveFilters(canvasFilters, widgetFilters);

  if (widget.type === "kpi") {
    return {
      dataset: widget.config.dataset,
      measure: widget.config.measure,
      dimensions: [],
      filters,
      limit: 1,
    };
  }

  // Bar widget.
  return {
    dataset: widget.config.dataset,
    measure: widget.config.measure,
    dimensions: widget.config.dimensions,
    filters,
    sort: widget.config.sort,
    limit: widget.config.limit ?? 10,
  };
}
