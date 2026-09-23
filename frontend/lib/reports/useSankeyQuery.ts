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
  FilterField,
  SankeyResponse,
  SankeyWidget,
} from "./types";

/**
 * TBD-552. The fields the Sankey endpoint accepts, as a KEEP-list. Inverted
 * from a ``txn_type``-only deny-list (see ``buildSankeyBody`` below) so an
 * unknown future catalog filter (the trap ``currency`` and ``transfer`` each
 * sprung once, TBD-507 / TBD-471) fails CLOSED — dropped rather than
 * forwarded to an endpoint whose ``SankeyQuery`` schema is ``extra="forbid"``.
 * ``txn_type`` is deliberately absent from both this list and the backend's
 * ``_SANKEY_DENIED_FILTER_FIELDS``: Sankey always aggregates every
 * transaction type for the income→spending flow, so it is its own
 * documented case, not a member of either set.
 *
 * ⚠ Mirrored on the backend by ``_SANKEY_SUPPORTED_FILTER_FIELDS``
 * (``sankey_service.py``) and asserted equal, both directions, by
 * ``backend/tests/test_reports_sankey_frontend_contract.py`` — parsed out
 * of this file, never grepped.
 */
export const SANKEY_SUPPORTED_FILTER_FIELDS: FilterField[] = [
  "date",
  "amount",
  "category_id",
  "account_id",
  "status",
  "tag_name",
];

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
  // override, canvas status cascade, account_ids, category_ids, txn_type,
  // amount_range, tag_names, the transfers axis. Sankey is always
  // transactions, which publishes both ``date`` and ``status``, so both
  // cascade here (pass ``true`` for each). Canvas status SHOULD scope
  // Sankey — it won't 422 and it keeps the cascade consistent.
  //
  // TBD-552: keep only the fields the Sankey endpoint accepts
  // (``SANKEY_SUPPORTED_FILTER_FIELDS``), inverted from a ``txn_type``-only
  // deny-list. ``txn_type`` is dropped here the same as before (the Sankey
  // endpoint ignores it — always aggregates every type for the
  // income→spending flow), but so now is anything the allowlist doesn't
  // name, which is the whole point: a future catalog field (``currency``,
  // ``transfer``) is dropped by default instead of silently forwarded to an
  // ``extra="forbid"`` endpoint.
  const resolvedFilters = resolveFilters(
    canvasFilters,
    widgetFilters,
    true, // transactions always supports date filter
    true, // transactions publishes status → canvas status scopes Sankey
  );
  const filters = resolvedFilters.filter((f) =>
    (SANKEY_SUPPORTED_FILTER_FIELDS as string[]).includes(f.field),
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
