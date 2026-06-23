/**
 * SWR-backed data hook for the Sankey cash-flow widget.
 *
 * Mirrors the structure of ``useReportQuery`` but targets the dedicated
 * ``POST /api/v1/reports/query/sankey`` endpoint whose wire contract differs
 * from the generic query endpoint:
 *
 *   - ``dataset`` and ``measure`` are implied by the backend (transactions +
 *     sum(amount)) and must NOT appear in the request body — the endpoint
 *     uses ``extra="forbid"`` and will 422 on extra keys.
 *   - The only accepted keys are ``filters``, ``spending_granularity``, and
 *     optionally ``top_n``.
 *
 * Filter resolution (canvas date cascade + per-widget override) reuses
 * ``resolveFilters`` from ``resolve.ts`` — identical logic to every other
 * widget hook; no duplication.
 */
import useSWR from "swr";
import { useMemo } from "react";

import { runSankeyQuery, type SankeyQueryBody } from "./api";
import { resolveFilters } from "./resolve";
import type {
  CanvasFilters,
  SankeyResponse,
  SankeyWidget,
} from "./types";

export interface UseSankeyQueryResult {
  data: SankeyResponse | undefined;
  error: Error | undefined;
  isLoading: boolean;
  /** The wire body that produced this result; useful for tests + debugging. */
  query: SankeyQueryBody;
}

/**
 * Builds the ``SankeyQuery`` wire body from a ``SankeyWidget`` config and the
 * shared canvas filters, then fetches via SWR. The SWR cache key is
 * ``["sankey-query", widgetId, JSON.stringify(body)]`` — widget.id is included
 * so each widget always gets its own cache entry even when configs are identical
 * (two Sankey widgets would otherwise race on a shared key and clobber each other).
 *
 * Canvas date cascades through ``resolveFilters`` exactly as it does for all
 * other widgets: the widget's ``filters.date_range`` overrides the canvas
 * value when set; otherwise the canvas date applies.
 */
export function useSankeyQuery(
  widget: SankeyWidget,
  canvasFilters: CanvasFilters | undefined,
): UseSankeyQueryResult {
  const query = useMemo<SankeyQueryBody>(
    () => buildSankeyBody(widget, canvasFilters),
    [widget, canvasFilters],
  );

  const swrKey = ["sankey-query", widget.id, JSON.stringify(query)];
  const { data, error, isLoading } = useSWR<SankeyResponse>(
    swrKey,
    () => runSankeyQuery(query),
    {
      revalidateOnFocus: false,
      revalidateIfStale: true,
      shouldRetryOnError: false,
    },
  );

  return {
    data,
    error: error as Error | undefined,
    isLoading: !!isLoading,
    query,
  };
}

/**
 * Pure builder — constructs the ``SankeyQueryBody`` from a widget config and
 * canvas filters. Exported so tests can assert on the exact wire body without
 * going through SWR.
 *
 * Key constraint: the Sankey endpoint uses ``extra="forbid"``, so only
 * ``filters``, ``spending_granularity``, and ``top_n`` may be present.
 * ``dataset`` and ``measure`` live on ``SankeyConfig`` for editor uniformity
 * but are deliberately excluded from the wire body here.
 */
export function buildSankeyBody(
  widget: SankeyWidget,
  canvasFilters: CanvasFilters | undefined,
): SankeyQueryBody {
  const widgetFilters = widget.config.filters;

  // Reuse the shared resolver — handles canvas date cascade, widget date
  // override, account_ids, category_ids, txn_type, amount_range, tag_names.
  // The sankey endpoint accepts any valid Filter[] so the full resolver
  // output is appropriate.
  const filters = resolveFilters(
    canvasFilters,
    widgetFilters,
    true, // transactions always supports date filter
  );

  const body: SankeyQueryBody = {
    filters,
    spending_granularity: widget.config.spending_granularity ?? "category",
  };

  // Only include top_n when the widget explicitly sets it (undefined must
  // not be serialised as ``null`` or sent at all — the backend field has
  // ``ge=1`` and will reject a null/0).
  if (widget.config.top_n !== undefined) {
    body.top_n = widget.config.top_n;
  }

  return body;
}
