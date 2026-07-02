/**
 * Filter-resolution utilities.
 *
 * Phase 4b scope: ``date_range`` is the ONLY canvas-shared field.
 * The shared canvas date cascades to every widget that doesn't
 * override it; accounts, categories, and all other filters are
 * widget-only. ``resolveFilters`` reads the widget value directly for
 * those and only inherits the date from the canvas.
 *
 * Output is a list of AST filter primitives the backend understands
 * (date BETWEEN, account_id IN, category_id IN, etc.). The compiler
 * server-side appends ``org_id = current_user.org_id`` and applies
 * the hard caps; this function only translates the user-facing UI
 * state into AST shape.
 */
import type {
  CanvasFilters,
  Dataset,
  Filter,
  SourceCatalogEntry,
  TxnType,
  WidgetFilters,
} from "./types";

/**
 * Coerce a persisted ``txn_type`` into a clean array. Old saved reports
 * stored it as a single string; new reports store an array. Filters out
 * unknown members and returns ``undefined`` when nothing valid remains,
 * so callers treat "no valid types" the same as "no filter".
 */
export function asTxnTypeArray(v: unknown): TxnType[] | undefined {
  if (v == null) return undefined;
  const arr = Array.isArray(v) ? v : [v];
  const out = arr.filter(
    (x): x is TxnType =>
      x === "income" || x === "expense" || x === "transfer",
  );
  return out.length > 0 ? out : undefined;
}

/**
 * Returns true when a widget-level field overrides the canvas-level
 * value. Used by the config rail to show the "Overrides canvas" pill.
 *
 * Locked rule: pill fires ONLY when BOTH the widget and the canvas
 * have a meaningful value for the field AND those values DIFFER.
 *
 * ``undefined`` / missing widget value = inherit (no pill).
 * Empty array / empty range = inherit (no pill).
 * Canvas has no value = widget-only, not an override (no pill).
 * Both set and equal = no override (no pill).
 * Both set and unequal = override (pill fires).
 */
export function isFieldOverridden(
  field: keyof WidgetFilters,
  widgetFilters: WidgetFilters | undefined,
  canvasFilters: CanvasFilters | undefined,
): boolean {
  if (!widgetFilters) return false;
  const widgetVal = widgetFilters[field];
  if (!hasMeaningfulValue(field, widgetVal)) return false;

  // Phase 4b: ``date_range`` is the ONLY field the canvas still shares.
  // Every other field (accounts, categories, tags, txn_type, amount)
  // is widget-only — the canvas can't hold it, so a widget value is
  // never an "override" of canvas. Short-circuit them all to false.
  if (field !== "date_range") {
    return false;
  }

  const canvasVal = canvasFilters?.[field as keyof CanvasFilters];
  if (!hasMeaningfulValue(field, canvasVal)) return false;

  // Both sides have a value. Pill fires only if they differ.
  return !valuesEqual(widgetVal, canvasVal);
}

function hasMeaningfulValue(
  field: keyof WidgetFilters,
  v: unknown,
): boolean {
  if (v === undefined || v === null) return false;
  if (Array.isArray(v)) return v.length > 0;
  if (field === "date_range") {
    const dr = v as { start?: string; end?: string };
    return Boolean(dr.start || dr.end);
  }
  if (field === "amount_range") {
    const ar = v as { min?: number; max?: number };
    return ar.min !== undefined || ar.max !== undefined;
  }
  return true;
}

// Phase 4b: ``date_range`` is the only field reaching this helper —
// ``isFieldOverridden`` short-circuits every other field to false
// before getting here. So this only needs the date comparison.
function valuesEqual(widgetVal: unknown, canvasVal: unknown): boolean {
  const a = widgetVal as { start?: string; end?: string };
  const b = canvasVal as { start?: string; end?: string };
  return (a.start ?? null) === (b.start ?? null)
    && (a.end ?? null) === (b.end ?? null);
}

/**
 * True when the data source ``dataset`` publishes a ``date`` filter
 * field in its catalog entry — i.e. the source has a date column the
 * canvas date range can scope. ``transactions`` does; ``accounts`` does
 * not.
 *
 * Defaults to ``true`` when the source can't be resolved (empty catalog
 * during the pre-load window, or a dataset missing from the catalog) so
 * we never silently drop the transactions date filter while the catalog
 * is still loading. The backend tolerates a stray date filter on a
 * date-less source (it drops it server-side), so "default to supports"
 * is the safe bias.
 */
export function sourceSupportsDateFilter(
  sources: SourceCatalogEntry[] | undefined,
  dataset: Dataset,
): boolean {
  if (!sources || sources.length === 0) return true;
  const entry = sources.find((s) => s.key === dataset);
  if (!entry) return true;
  return entry.filters.some((f) => f.field === "date");
}

/**
 * Resolves a widget's effective filters into a list of AST filter
 * primitives. ``widget`` overrides on a per-field basis; otherwise
 * the canvas value cascades through.
 *
 * ``sourceSupportsDate`` gates the shared canvas date filter: when
 * ``false`` (a date-less source such as ``accounts``), the date range
 * is omitted entirely so we never send a filter the source can't
 * honor. Defaults to ``true`` to preserve current behavior when the
 * source catalog isn't available yet.
 */
export function resolveFilters(
  canvas: CanvasFilters | undefined,
  widget: WidgetFilters | undefined,
  sourceSupportsDate = true,
): Filter[] {
  const out: Filter[] = [];
  const canvasDr = canvas?.date_range;
  const widgetDr = widget?.date_range;
  const dr = sourceSupportsDate ? pickDateRange(widgetDr, canvasDr) : undefined;
  if (dr && dr.start && dr.end) {
    out.push({
      field: "date",
      op: "between",
      value: [dr.start, dr.end],
    });
  } else if (dr && dr.start) {
    out.push({ field: "date", op: "gte", value: dr.start });
  } else if (dr && dr.end) {
    out.push({ field: "date", op: "lte", value: dr.end });
  }

  // Phase 4b: accounts/categories are widget-only — read the widget
  // value directly, no canvas fallback.
  const accountIds = widget?.account_ids;
  if (accountIds && accountIds.length > 0) {
    out.push({ field: "account_id", op: "in", value: accountIds });
  }

  const categoryIds = widget?.category_ids;
  if (categoryIds && categoryIds.length > 0) {
    out.push({ field: "category_id", op: "in", value: categoryIds });
  }

  const txnTypes = asTxnTypeArray(widget?.txn_type);
  if (txnTypes) {
    out.push({ field: "txn_type", op: "in", value: txnTypes });
  }

  // Settled/Pending status — widget-only (like txn_type). Omitted
  // entirely for the "All" choice (``status`` undefined). The backend
  // ``FilterField.STATUS`` coerces the value to its enum server-side.
  if (widget?.status) {
    out.push({ field: "status", op: "eq", value: widget.status });
  }

  if (widget?.amount_range) {
    const { min, max } = widget.amount_range;
    if (min !== undefined && max !== undefined) {
      out.push({ field: "amount", op: "between", value: [min, max] });
    } else if (min !== undefined) {
      out.push({ field: "amount", op: "gte", value: min });
    } else if (max !== undefined) {
      out.push({ field: "amount", op: "lte", value: max });
    }
  }

  if (widget?.tag_names && widget.tag_names.length > 0) {
    // One ``in`` filter with the full tag list. The backend's tag
    // compiler at ``backend/app/services/reports_query_service.py:185``
    // reads ``tag_match`` off the single filter and either OR-combines
    // the names (``any``) or AND-combines per-name IN subqueries
    // (``all``). Emitting per-tag filters here would force the AST
    // compiler to AND them together, which inverts the UI promise
    // for ``tag_match=any``.
    out.push({
      field: "tag_name",
      op: "in",
      value: [...widget.tag_names],
      tag_match: widget.tag_match ?? "all",
    });
  }

  return out;
}

/**
 * Maps each ``WidgetFilters`` key to the backend filter ``field`` it
 * compiles to (see ``resolveFilters`` above for the actual emission).
 * The single source of truth for the WidgetFilters↔source-field
 * mapping, reused by ``pruneFiltersToSource`` so the prune logic can
 * never drift from what the resolver emits.
 *
 * ``tag_match`` is a knob on the ``tag_name`` filter (not its own
 * field), so it shares ``tag_name``'s mapping and is pruned in lockstep
 * with ``tag_names``.
 */
const FILTER_KEY_TO_SOURCE_FIELD: Record<keyof WidgetFilters, string> = {
  date_range: "date",
  account_ids: "account_id",
  category_ids: "category_id",
  txn_type: "txn_type",
  status: "status",
  amount_range: "amount",
  tag_names: "tag_name",
  tag_match: "tag_name",
};

/**
 * Prunes a widget's per-widget ``WidgetFilters`` down to only the
 * filters the given source publishes. Any key whose backend filter
 * field isn't in ``publishedFields`` is dropped, so switching a widget's
 * source (e.g. transactions → accounts) can't strand a filter the new
 * source's ``validate()`` would 422 (a leftover ``category_ids`` /
 * ``txn_type`` / ``amount_range`` on an accounts widget).
 *
 * ``publishedFields`` is the new source's published filter fields —
 * ``entry.filters.map((f) => f.field)``. Returns a NEW object (never
 * mutates the input); returns ``undefined`` when the pruned result is
 * empty so we don't persist an empty ``{}`` filters blob.
 */
export function pruneFiltersToSource(
  filters: WidgetFilters | undefined,
  publishedFields: string[],
): WidgetFilters | undefined {
  if (!filters) return undefined;
  const allowed = new Set(publishedFields);
  const out: WidgetFilters = {};
  let kept = 0;
  for (const key of Object.keys(filters) as Array<keyof WidgetFilters>) {
    const field = FILTER_KEY_TO_SOURCE_FIELD[key];
    if (!field || !allowed.has(field)) continue;
    // ``tag_match`` rides along only when ``tag_names`` survives — its
    // field is ``tag_name``, already gated by the same membership check.
    Object.assign(out, { [key]: filters[key] });
    kept += 1;
  }
  return kept > 0 ? out : undefined;
}

/**
 * The single source of truth for the date inherit/override decision:
 * the widget date wins when it has a start or end, otherwise the
 * shared canvas date cascades. Exported so the chip-describe helper
 * (``describe-filters.ts``) reuses this exact logic rather than
 * reimplementing it.
 */
export function pickDateRange(
  widget: CanvasFilters["date_range"] | undefined,
  canvas: CanvasFilters["date_range"] | undefined,
): CanvasFilters["date_range"] | undefined {
  if (widget && (widget.start || widget.end)) return widget;
  return canvas;
}
