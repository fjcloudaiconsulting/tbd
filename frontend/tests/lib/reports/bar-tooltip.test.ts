import { describe, expect, it } from "vitest";

import { makeReportBarTooltipResolver } from "@/lib/reports/bar-tooltip";

describe("makeReportBarTooltipResolver — sliced (secondary-dimension breakdown)", () => {
  const resolve = makeReportBarTooltipResolver({
    sliced: true,
    seriesKeys: ["s0", "s1", "s2"],
    secondaryValues: ["Rent", "Groceries", "Salary"],
    sliceColors: ["var(--color-chart-1)", "var(--color-chart-2)", "var(--color-chart-3)"],
    valueName: "Amount",
    singleColor: "var(--color-chart-spent)",
  });

  it("maps a hovered series key to its secondary label + colour", () => {
    expect(resolve({ dataKey: "s1", value: 42 })).toEqual({
      label: "Groceries",
      color: "var(--color-chart-2)",
    });
  });

  it("drops a backfilled-zero series so the tooltip omits categories not in the hovered group", () => {
    // s2 (Salary) is 0 for this bar because it belongs to another group and
    // the pivot backfilled it — it must NOT appear in the tooltip.
    expect(resolve({ dataKey: "s2", value: 0 })).toBeNull();
  });

  it("returns null for an unknown dataKey", () => {
    expect(resolve({ dataKey: "s9", value: 10 })).toBeNull();
  });
});

describe("makeReportBarTooltipResolver — single bar (no breakdown)", () => {
  const resolve = makeReportBarTooltipResolver({
    sliced: false,
    seriesKeys: [],
    secondaryValues: [],
    sliceColors: [],
    valueName: "Amount",
    singleColor: "var(--color-chart-spent)",
  });

  it("labels the single value bar with valueName + single colour", () => {
    expect(resolve({ dataKey: "value", value: 100 })).toEqual({
      label: "Amount",
      color: "var(--color-chart-spent)",
    });
  });

  it("keeps a single bar whose value is zero (a real measured zero, not backfill)", () => {
    expect(resolve({ dataKey: "value", value: 0 })).toEqual({
      label: "Amount",
      color: "var(--color-chart-spent)",
    });
  });

  it("returns null for any other dataKey on a single bar", () => {
    expect(resolve({ dataKey: "s0", value: 5 })).toBeNull();
  });
});
