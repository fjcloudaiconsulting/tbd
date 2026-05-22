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
 * ``undefined`` / missing widget value = inherit. Empty array = inherit.
 * Any other non-empty value = override.
 */
export function isFieldOverridden(
  field: keyof WidgetFilters,
  widgetFilters: WidgetFilters | undefined,
  canvasFilters: CanvasFilters | undefined,
): boolean {
  if (!widgetFilters) return false;
  const v = widgetFilters[field];
  if (v === undefined || v === null) return false;
  if (Array.isArray(v) && v.length === 0) return false;
  // Date range with neither start nor end is treated as "inherit."
  if (field === "date_range") {
    const dr = v as { start?: string; end?: string };
    if (!dr.start && !dr.end) return false;
  }
  // Amount range with neither min nor max is "inherit."
  if (field === "amount_range") {
    const ar = v as { min?: number; max?: number };
    if (ar.min === undefined && ar.max === undefined) return false;
  }
  // If the corresponding canvas value is also unset, the widget
  // value isn't really an override — it's just a widget-only setting.
  // For pill rendering purposes we still call it "overrides canvas"
  // when the canvas had a value AND the widget value differs.
  const c = canvasFilters?.[field as keyof CanvasFilters];
  if (c === undefined || c === null) return false;
  if (Array.isArray(c) && c.length === 0) return false;
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
    for (const tag of widget.tag_names) {
      out.push({
        field: "tag_name",
        op: "eq",
        value: tag,
        tag_match: widget.tag_match ?? "all",
      });
    }
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
