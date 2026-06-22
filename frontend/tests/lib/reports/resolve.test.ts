import { describe, expect, it } from "vitest";

import {
  asTxnTypeArray,
  isFieldOverridden,
  pickDateRange,
  resolveFilters,
} from "@/lib/reports/resolve";
import type { CanvasFilters, WidgetFilters } from "@/lib/reports/types";

describe("asTxnTypeArray", () => {
  it("coerces a legacy string value to a one-element array", () => {
    expect(asTxnTypeArray("income")).toEqual(["income"]);
  });
  it("passes a valid array through", () => {
    expect(asTxnTypeArray(["income", "expense"])).toEqual(["income", "expense"]);
  });
  it("drops unknown members and returns undefined when empty", () => {
    expect(asTxnTypeArray(["bogus"])).toBeUndefined();
    expect(asTxnTypeArray([])).toBeUndefined();
    expect(asTxnTypeArray(undefined)).toBeUndefined();
    expect(asTxnTypeArray(null)).toBeUndefined();
  });
});

describe("resolveFilters txn_type", () => {
  it("emits op:in for a multi-select array", () => {
    const out = resolveFilters(undefined, {
      txn_type: ["income", "expense"],
    } as WidgetFilters);
    expect(out).toContainEqual({
      field: "txn_type",
      op: "in",
      value: ["income", "expense"],
    });
  });
  it("emits nothing for an empty or undefined selection", () => {
    expect(
      resolveFilters(undefined, { txn_type: [] } as unknown as WidgetFilters),
    ).not.toContainEqual(expect.objectContaining({ field: "txn_type" }));
    expect(resolveFilters(undefined, {})).not.toContainEqual(
      expect.objectContaining({ field: "txn_type" }),
    );
  });
  it("coerces a legacy string txn_type to op:in", () => {
    const out = resolveFilters(undefined, {
      txn_type: "expense",
    } as unknown as WidgetFilters);
    expect(out).toContainEqual({
      field: "txn_type",
      op: "in",
      value: ["expense"],
    });
  });
});

/**
 * Phase 4b model: ``date_range`` is the ONLY canvas-shared field, so
 * it's the only field that can be an "override". Accounts and
 * categories are now widget-only — ``CanvasFilters`` no longer carries
 * them — so ``isFieldOverridden`` must always return false for them.
 *
 * The date_range cases pin the five canonical override cases:
 *   1. identical -> no pill
 *   2. different by one element -> pill
 *   3. partial overlap -> pill
 *   4. canvas-only -> no pill
 *   5. widget-only -> no pill
 */
describe("isFieldOverridden", () => {
  describe("date_range", () => {
    it("returns false when widget and canvas date ranges are identical", () => {
      const canvas: CanvasFilters = {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      };
      const widget: WidgetFilters = {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      };
      expect(isFieldOverridden("date_range", widget, canvas)).toBe(false);
    });

    it("returns true when only the end date differs", () => {
      const canvas: CanvasFilters = {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      };
      const widget: WidgetFilters = {
        date_range: { start: "2026-01-01", end: "2026-02-15" },
      };
      expect(isFieldOverridden("date_range", widget, canvas)).toBe(true);
    });

    it("returns true when only the start date differs", () => {
      const canvas: CanvasFilters = {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      };
      const widget: WidgetFilters = {
        date_range: { start: "2026-01-05", end: "2026-01-31" },
      };
      expect(isFieldOverridden("date_range", widget, canvas)).toBe(true);
    });

    it("returns false when canvas has a date range but the widget doesn't", () => {
      const canvas: CanvasFilters = {
        date_range: { start: "2026-01-01", end: "2026-01-31" },
      };
      const widget: WidgetFilters = {};
      expect(isFieldOverridden("date_range", widget, canvas)).toBe(false);
    });

    it("returns false when only the widget has a date range (no canvas value)", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = {
        date_range: { start: "2026-02-01", end: "2026-02-15" },
      };
      expect(isFieldOverridden("date_range", widget, canvas)).toBe(false);
    });
  });

  describe("account_ids (widget-only, never a canvas override)", () => {
    // Accounts no longer live on the canvas, so a widget account list
    // is never an "override" — the pill must never fire for it.
    it("returns false even when a widget account list is set", () => {
      const widget: WidgetFilters = { account_ids: [1, 2] };
      expect(isFieldOverridden("account_ids", widget, {})).toBe(false);
    });

    it("returns false for an empty widget account list", () => {
      const widget: WidgetFilters = { account_ids: [] };
      expect(isFieldOverridden("account_ids", widget, {})).toBe(false);
    });
  });

  describe("category_ids (widget-only, never a canvas override)", () => {
    it("returns false even when a widget category list is set", () => {
      const widget: WidgetFilters = { category_ids: [10, 20] };
      expect(isFieldOverridden("category_ids", widget, {})).toBe(false);
    });

    it("returns false for an empty widget category list", () => {
      const widget: WidgetFilters = { category_ids: [] };
      expect(isFieldOverridden("category_ids", widget, {})).toBe(false);
    });
  });

  describe("widget-only fields (txn_type, amount_range, tag_names, tag_match)", () => {
    // These fields don't exist on CanvasFilters at all, so a widget
    // value can never "override" a canvas value for them. The pill
    // must never render for these fields.
    it("never reports txn_type as overriding canvas", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { txn_type: "expense" };
      expect(isFieldOverridden("txn_type", widget, canvas)).toBe(false);
    });

    it("never reports amount_range as overriding canvas", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { amount_range: { min: 0, max: 100 } };
      expect(isFieldOverridden("amount_range", widget, canvas)).toBe(false);
    });

    it("never reports tag_names as overriding canvas", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { tag_names: ["groceries"] };
      expect(isFieldOverridden("tag_names", widget, canvas)).toBe(false);
    });

    it("never reports tag_match as overriding canvas", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { tag_match: "any" };
      expect(isFieldOverridden("tag_match", widget, canvas)).toBe(false);
    });
  });
});

/**
 * Architect bug fix: the previous emitter looped over ``tag_names`` and
 * pushed one ``{op: "eq"}`` filter per tag. The AST compiler AND-combines
 * filters, so ``tag_match="any"`` with N tags actually ran as "match ALL"
 * — the opposite of the UI promise. The canonical shape is ONE ``in``
 * filter carrying the full list, with ``tag_match`` riding on that single
 * filter. Backend reference:
 * ``backend/app/services/reports_query_service.py:185``.
 */
describe("resolveFilters — tag emission", () => {
  it("emits a single 'in' filter with one tag when tag_match='any' and 1 tag is selected", () => {
    const widget: WidgetFilters = {
      tag_names: ["groceries"],
      tag_match: "any",
    };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    expect(tagFilters).toHaveLength(1);
    expect(tagFilters[0]).toEqual({
      field: "tag_name",
      op: "in",
      value: ["groceries"],
      tag_match: "any",
    });
  });

  it("emits ONE filter (not N) with the full tag list when tag_match='any' and 3 tags are selected", () => {
    const widget: WidgetFilters = {
      tag_names: ["groceries", "essentials", "treat"],
      tag_match: "any",
    };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    // The bug: previously this would have been 3 separate eq filters,
    // which the AST compiler AND-combines — collapsing "any" into "all".
    expect(tagFilters).toHaveLength(1);
    expect(tagFilters[0]).toEqual({
      field: "tag_name",
      op: "in",
      value: ["groceries", "essentials", "treat"],
      tag_match: "any",
    });
  });

  it("emits a single 'in' filter with one tag when tag_match='all' and 1 tag is selected", () => {
    const widget: WidgetFilters = {
      tag_names: ["essentials"],
      tag_match: "all",
    };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    expect(tagFilters).toHaveLength(1);
    expect(tagFilters[0]).toEqual({
      field: "tag_name",
      op: "in",
      value: ["essentials"],
      tag_match: "all",
    });
  });

  it("emits ONE filter with the full tag list when tag_match='all' and 3 tags are selected", () => {
    const widget: WidgetFilters = {
      tag_names: ["groceries", "essentials", "treat"],
      tag_match: "all",
    };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    // The backend's tag compiler handles ``tag_match=all`` server-side
    // by AND-combining per-name IN subqueries; the wire shape stays
    // ONE filter with the full list.
    expect(tagFilters).toHaveLength(1);
    expect(tagFilters[0]).toEqual({
      field: "tag_name",
      op: "in",
      value: ["groceries", "essentials", "treat"],
      tag_match: "all",
    });
  });

  it("emits no tag filter when tag_names is an empty array", () => {
    const widget: WidgetFilters = {
      tag_names: [],
      tag_match: "any",
    };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    expect(tagFilters).toHaveLength(0);
  });

  it("defaults tag_match to 'all' when only tag_names is set", () => {
    const widget: WidgetFilters = { tag_names: ["essentials"] };
    const out = resolveFilters(undefined, widget);
    const tagFilters = out.filter((f) => f.field === "tag_name");
    expect(tagFilters).toHaveLength(1);
    expect(tagFilters[0]).toEqual({
      field: "tag_name",
      op: "in",
      value: ["essentials"],
      tag_match: "all",
    });
  });
});

describe("resolveFilters — widget-only accounts/categories", () => {
  it("emits account_id/category_id filters from the WIDGET (canvas no longer contributes)", () => {
    const out = resolveFilters(
      { date_range: { start: "2026-01-01", end: "2026-01-31" } },
      { account_ids: [7], category_ids: [9] },
    );
    expect(out).toContainEqual({ field: "account_id", op: "in", value: [7] });
    expect(out).toContainEqual({ field: "category_id", op: "in", value: [9] });
  });

  it("does NOT propagate canvas-level account_ids/category_ids (phase-4b model guard)", () => {
    // Defensive against a regression that reintroduces a canvas fallback.
    // CanvasFilters no longer types these, but a legacy/casted JSON blob
    // could still carry them — they must be ignored entirely. Only the
    // widget contributes account/category filters.
    const canvasWithStaleIds = {
      date_range: { start: "2026-01-01", end: "2026-01-31" },
      // Cast through unknown: these keys are NOT on CanvasFilters by design.
      account_ids: [101, 102],
      category_ids: [201],
    } as unknown as CanvasFilters;

    const out = resolveFilters(canvasWithStaleIds, {});
    // The date still resolves from the canvas...
    expect(out).toContainEqual({
      field: "date",
      op: "between",
      value: ["2026-01-01", "2026-01-31"],
    });
    // ...but NO account/category filter is emitted from canvas-level ids.
    expect(out.some((f) => f.field === "account_id")).toBe(false);
    expect(out.some((f) => f.field === "category_id")).toBe(false);
  });

  it("emits ONLY the widget account ids even when canvas also carries (stale) ids", () => {
    const canvasWithStaleIds = {
      account_ids: [101],
      category_ids: [201],
    } as unknown as CanvasFilters;

    const out = resolveFilters(canvasWithStaleIds, { account_ids: [7] });
    expect(out).toContainEqual({ field: "account_id", op: "in", value: [7] });
    // The canvas id 101 must never appear.
    expect(out).not.toContainEqual({
      field: "account_id",
      op: "in",
      value: [101],
    });
    expect(out.some((f) => f.field === "category_id")).toBe(false);
  });
});

describe("pickDateRange (exported single source of truth)", () => {
  it("prefers the widget date, falls back to canvas", () => {
    expect(
      pickDateRange({ start: "2026-02-01" }, { start: "2026-01-01" }),
    ).toEqual({ start: "2026-02-01" });
    expect(pickDateRange(undefined, { start: "2026-01-01" })).toEqual({
      start: "2026-01-01",
    });
  });

  it("falls back to canvas when the widget range is empty (no start/end)", () => {
    expect(pickDateRange({}, { start: "2026-01-01" })).toEqual({
      start: "2026-01-01",
    });
  });

  it("returns undefined when neither widget nor canvas has a date", () => {
    expect(pickDateRange(undefined, undefined)).toBeUndefined();
  });

  it("returns undefined when the widget range is empty and there is no canvas", () => {
    expect(pickDateRange({}, undefined)).toBeUndefined();
  });
});
