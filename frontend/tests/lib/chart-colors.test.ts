import { describe, it, expect } from "vitest";
import { budgetBarFill, CHART_SERIES, chartColor } from "@/lib/chart-colors";

describe("CHART_SERIES", () => {
  it("exposes 8 token-based categorical colors", () => {
    expect(CHART_SERIES).toHaveLength(8);
    CHART_SERIES.forEach((c, i) =>
      expect(c).toBe(`var(--color-chart-${i + 1})`)
    );
  });
});

describe("budgetBarFill (TBD-556)", () => {
  it("paints a 0-amount budget with spend as over budget", () => {
    // FENCE: percent_used is 0 at amount 0, so a `percent_used > 100` check
    // paints this row as ordinary spend.
    expect(budgetBarFill({ percent_used: 0, over_budget: true })).toBe(chartColor.over);
  });

  it("keeps the watch and spent bands for budgets under their amount", () => {
    expect(budgetBarFill({ percent_used: 90, over_budget: false })).toBe(chartColor.watch);
    expect(budgetBarFill({ percent_used: 40, over_budget: false })).toBe(chartColor.spent);
  });
});
