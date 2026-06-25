import { describe, it, expect } from "vitest";
import { cloneWidgetForDashboard } from "../../../lib/dashboard/clone";
import type { Widget } from "../../../lib/reports/types";

// Minimal fixture cast through unknown — BarConfig requires `dimensions: Dimension[]`
// but the test only cares about deep-copy isolation, not config validity.
const source = {
  id: "w_src",
  type: "bar",
  title: "Spend by category",
  grid: { x: 3, y: 2, w: 6, h: 4 },
  config: { dataset: "transactions", measure: { agg: "sum", field: "amount" }, dimension: "category" },
} as unknown as Widget;

describe("cloneWidgetForDashboard", () => {
  it("gives the clone a fresh id but preserves type/title/config", () => {
    const clone = cloneWidgetForDashboard(source, []);
    expect(clone.id).not.toBe(source.id);
    expect(clone.type).toBe("bar");
    expect(clone.title).toBe("Spend by category");
    expect(clone.config).toEqual(source.config);
  });

  it("deep-copies config (mutating the clone never touches the source)", () => {
    const clone = cloneWidgetForDashboard(source, []);
    (clone.config as unknown as Record<string, unknown>).dimension = "merchant";
    expect((source.config as unknown as Record<string, unknown>).dimension).toBe("category");
  });

  it("places the clone below all existing widgets, preserving its w/h", () => {
    const existing = [
      { id: "a", type: "kpi", title: "x", grid: { x: 0, y: 0, w: 4, h: 3 }, config: {} },
    ] as unknown as Widget[];
    const clone = cloneWidgetForDashboard(source, existing);
    expect(clone.grid).toEqual({ x: 0, y: 3, w: 6, h: 4 });
  });

  it("places at row 0 when there are no existing widgets", () => {
    const clone = cloneWidgetForDashboard(source, []);
    expect(clone.grid.y).toBe(0);
  });
});
