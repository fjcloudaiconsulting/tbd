import { describe, it, expect } from "vitest";
import { CHART_SERIES } from "@/lib/chart-colors";

describe("CHART_SERIES", () => {
  it("exposes 8 token-based categorical colors", () => {
    expect(CHART_SERIES).toHaveLength(8);
    CHART_SERIES.forEach((c, i) =>
      expect(c).toBe(`var(--color-chart-${i + 1})`)
    );
  });
});
