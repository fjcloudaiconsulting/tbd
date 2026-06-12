import { describe, it, expect } from "vitest";

import {
  resolveForecastSeries,
  resolveBudgetSeries,
} from "@/lib/reports/chart-series-tooltip";
import { chartColor } from "@/lib/chart-colors";

describe("resolveForecastSeries", () => {
  it("maps planned to the planned colour", () => {
    expect(resolveForecastSeries({ dataKey: "planned", value: 100 })).toEqual({
      label: "Planned",
      color: chartColor.planned,
    });
  });

  it("colours actual green when at/under plan", () => {
    expect(
      resolveForecastSeries({
        dataKey: "actual",
        value: 80,
        payload: { planned: 100, actual: 80 },
      }),
    ).toEqual({ label: "Actual", color: chartColor.actual });
  });

  it("colours actual red when over plan (matches the bar's Cell fill)", () => {
    expect(
      resolveForecastSeries({
        dataKey: "actual",
        value: 120,
        payload: { planned: 100, actual: 120 },
      }),
    ).toEqual({ label: "Actual", color: chartColor.over });
  });

  it("returns null for an unknown series", () => {
    expect(resolveForecastSeries({ dataKey: "ghost", value: 1 })).toBeNull();
  });
});

describe("resolveBudgetSeries", () => {
  it("omits zero-value stacked segments (no 'Over budget: $0.00' noise)", () => {
    expect(resolveBudgetSeries({ dataKey: "over", value: 0 })).toBeNull();
    expect(resolveBudgetSeries({ dataKey: "remaining", value: 0 })).toBeNull();
    expect(resolveBudgetSeries({ dataKey: "spent", value: 0 })).toBeNull();
  });

  it("walks the spent swatch through the utilisation tiers via pct (dashboard datum)", () => {
    expect(
      resolveBudgetSeries({ dataKey: "spent", value: 50, payload: { pct: 50 } })?.color,
    ).toBe(chartColor.spent);
    expect(
      resolveBudgetSeries({ dataKey: "spent", value: 90, payload: { pct: 90 } })?.color,
    ).toBe(chartColor.watch);
    expect(
      resolveBudgetSeries({ dataKey: "spent", value: 110, payload: { pct: 110 } })?.color,
    ).toBe(chartColor.over);
  });

  it("derives the tier from over/spent/remaining when pct is absent (budgets-page datum)", () => {
    expect(
      resolveBudgetSeries({
        dataKey: "spent",
        value: 100,
        payload: { spent: 100, remaining: 0, over: 20 },
      })?.color,
    ).toBe(chartColor.over); // over > 0 → over-budget tier
    expect(
      resolveBudgetSeries({
        dataKey: "spent",
        value: 90,
        payload: { spent: 90, remaining: 10, over: 0 },
      })?.color,
    ).toBe(chartColor.watch); // 90% → watch
    expect(
      resolveBudgetSeries({
        dataKey: "spent",
        value: 50,
        payload: { spent: 50, remaining: 50, over: 0 },
      })?.color,
    ).toBe(chartColor.spent); // 50% → normal
  });

  it("labels non-zero over and remaining with their colours", () => {
    expect(resolveBudgetSeries({ dataKey: "over", value: 20 })).toEqual({
      label: "Over budget",
      color: chartColor.over,
    });
    expect(resolveBudgetSeries({ dataKey: "remaining", value: 30 })).toEqual({
      label: "Remaining",
      color: chartColor.remaining,
    });
  });
});
