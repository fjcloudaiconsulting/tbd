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
  Filter,
  WidgetFilters,
} from "./types";

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
 * Resolves a widget's effective filters into a list of AST filter
 * primitives. ``widget`` overrides on a per-field basis; otherwise
 * the canvas value cascades through.
 */
export function resolveFilters(
  canvas: CanvasFilters | undefined,
  widget: WidgetFilters | undefined,
): Filter[] {
  const out: Filter[] = [];
  const canvasDr = canvas?.date_range;
  const widgetDr = widget?.date_range;
  const dr = pickDateRange(widgetDr, canvasDr);
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

  if (widget?.txn_type) {
    out.push({ field: "txn_type", op: "eq", value: widget.txn_type });
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
