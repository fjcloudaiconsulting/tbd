import { describe, expect, it } from "vitest";

import { isFieldOverridden, resolveFilters } from "@/lib/reports/resolve";
import type { CanvasFilters, WidgetFilters } from "@/lib/reports/types";

/**
 * Locked rule (spec §4): the "Overrides canvas" pill fires ONLY
 * when BOTH widget and canvas have a meaningful value AND the
 * values differ. Identical values must NOT show the pill.
 *
 * These tests pin the five canonical cases across each field type:
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

  describe("account_ids", () => {
    it("returns false when both lists contain the same ids regardless of order", () => {
      const canvas: CanvasFilters = { account_ids: [3, 1, 2] };
      const widget: WidgetFilters = { account_ids: [1, 2, 3] };
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(false);
    });

    it("returns true when the widget list has an extra id", () => {
      const canvas: CanvasFilters = { account_ids: [1, 2] };
      const widget: WidgetFilters = { account_ids: [1, 2, 3] };
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(true);
    });

    it("returns true when the lists partially overlap", () => {
      const canvas: CanvasFilters = { account_ids: [1, 2, 3] };
      const widget: WidgetFilters = { account_ids: [2, 3, 4] };
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(true);
    });

    it("returns false when canvas has account_ids but the widget doesn't", () => {
      const canvas: CanvasFilters = { account_ids: [1, 2, 3] };
      const widget: WidgetFilters = {};
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(false);
    });

    it("returns false when only the widget has account_ids (no canvas value)", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { account_ids: [5] };
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(false);
    });

    it("treats an empty widget account_ids array as inherit (no pill)", () => {
      const canvas: CanvasFilters = { account_ids: [1, 2, 3] };
      const widget: WidgetFilters = { account_ids: [] };
      expect(isFieldOverridden("account_ids", widget, canvas)).toBe(false);
    });
  });

  describe("category_ids", () => {
    it("returns false when both lists contain the same ids regardless of order", () => {
      const canvas: CanvasFilters = { category_ids: [10, 20, 30] };
      const widget: WidgetFilters = { category_ids: [30, 20, 10] };
      expect(isFieldOverridden("category_ids", widget, canvas)).toBe(false);
    });

    it("returns true when one id differs", () => {
      const canvas: CanvasFilters = { category_ids: [10, 20] };
      const widget: WidgetFilters = { category_ids: [10, 21] };
      expect(isFieldOverridden("category_ids", widget, canvas)).toBe(true);
    });

    it("returns true on partial overlap", () => {
      const canvas: CanvasFilters = { category_ids: [10, 20, 30] };
      const widget: WidgetFilters = { category_ids: [20, 30, 40] };
      expect(isFieldOverridden("category_ids", widget, canvas)).toBe(true);
    });

    it("returns false when canvas has category_ids but the widget doesn't", () => {
      const canvas: CanvasFilters = { category_ids: [10, 20] };
      const widget: WidgetFilters = {};
      expect(isFieldOverridden("category_ids", widget, canvas)).toBe(false);
    });

    it("returns false when only the widget has category_ids", () => {
      const canvas: CanvasFilters = {};
      const widget: WidgetFilters = { category_ids: [10] };
      expect(isFieldOverridden("category_ids", widget, canvas)).toBe(false);
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
