/**
 * Filter-resolution utilities.
 *
 * The architect-locked hybrid scope (spec §4): canvas-wide cascades,
 * per-widget overrides win on a per-field basis. ``resolveFilters``
 * walks each widget-filter field and decides whether to use the
 * widget value (if present) or fall back to the canvas value.
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

  // Tag fields aren't on canvas at all (canvas only has date_range,
  // account_ids, category_ids), so a widget value there isn't an
  // override of canvas. Same for txn_type / amount_range.
  if (
    field === "tag_names" ||
    field === "tag_match" ||
    field === "txn_type" ||
    field === "amount_range"
  ) {
    return false;
  }

  const canvasVal = canvasFilters?.[field as keyof CanvasFilters];
  if (!hasMeaningfulValue(field, canvasVal)) return false;

  // Both sides have a value. Pill fires only if they differ.
  return !valuesEqual(field, widgetVal, canvasVal, widgetFilters);
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

function valuesEqual(
  field: keyof WidgetFilters,
  widgetVal: unknown,
  canvasVal: unknown,
  widgetFilters: WidgetFilters,
): boolean {
  if (field === "date_range") {
    const a = widgetVal as { start?: string; end?: string };
    const b = canvasVal as { start?: string; end?: string };
    return (a.start ?? null) === (b.start ?? null)
      && (a.end ?? null) === (b.end ?? null);
  }
  if (field === "account_ids" || field === "category_ids") {
    return sameSet(widgetVal as number[], canvasVal as number[]);
  }
  if (field === "amount_range") {
    const a = widgetVal as { min?: number; max?: number };
    const b = canvasVal as { min?: number; max?: number };
    return (a.min ?? null) === (b.min ?? null)
      && (a.max ?? null) === (b.max ?? null);
  }
  if (field === "tag_names") {
    if (!sameSet(widgetVal as string[], canvasVal as string[])) return false;
    // tag_match flips "all" vs "any" semantics, so an identical tag
    // list with a different match mode is still an override. Canvas
    // doesn't model tag_match, so any widget-side tag_match value
    // other than the implicit default counts as a difference.
    const widgetMatch = widgetFilters.tag_match ?? "all";
    return widgetMatch === "all";
  }
  // txn_type / tag_match handled above as widget-only; fall back to
  // strict equality for safety.
  return widgetVal === canvasVal;
}

function sameSet<T extends number | string>(a: T[], b: T[]): boolean {
  if (a.length !== b.length) return false;
  const bSet = new Set<T>(b);
  for (const x of a) {
    if (!bSet.has(x)) return false;
  }
  return true;
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

  const accountIds = pickList(widget?.account_ids, canvas?.account_ids);
  if (accountIds && accountIds.length > 0) {
    out.push({ field: "account_id", op: "in", value: accountIds });
  }

  const categoryIds = pickList(widget?.category_ids, canvas?.category_ids);
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

function pickDateRange(
  widget: CanvasFilters["date_range"] | undefined,
  canvas: CanvasFilters["date_range"] | undefined,
): CanvasFilters["date_range"] | undefined {
  if (widget && (widget.start || widget.end)) return widget;
  return canvas;
}

function pickList<T>(
  widget: T[] | undefined,
  canvas: T[] | undefined,
): T[] | undefined {
  if (widget && widget.length > 0) return widget;
  return canvas;
}
