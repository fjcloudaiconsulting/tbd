import { describe, it, expect } from "vitest";

import { formatMeasureValue } from "@/lib/reports/series";
import { formatAmount } from "@/lib/format";

describe("formatMeasureValue", () => {
  it("currency → grouped, 2 decimals, no currency symbol", () => {
    // Compare against formatAmount (the same fn the app uses) rather than a
    // hardcoded "1,234.56" so the test is locale-independent.
    const out = formatMeasureValue(1234.56, "currency");
    expect(out).toBe(formatAmount(1234.56));
    // Currency symbols are deferred to the future multi-currency work.
    expect(out).not.toContain("$");
    expect(out).not.toContain("USD");
  });

  it("percent → one-decimal percentage (toFixed is locale-independent)", () => {
    expect(formatMeasureValue(12.3, "percent")).toBe("12.3%");
    expect(formatMeasureValue(12.34, "percent")).toBe("12.3%");
    expect(formatMeasureValue(0, "percent")).toBe("0.0%");
  });

  it("number → grouped via toLocaleString", () => {
    expect(formatMeasureValue(1234, "number")).toBe((1234).toLocaleString());
    expect(formatMeasureValue(1234567, "number")).toBe((1234567).toLocaleString());
  });

  it("handles zero and negative values", () => {
    expect(formatMeasureValue(0, "currency")).toBe(formatAmount(0));
    expect(formatMeasureValue(-1234.5, "currency")).toBe(formatAmount(-1234.5));
    expect(formatMeasureValue(-50, "number")).toBe((-50).toLocaleString());
  });

  it('returns "" for non-finite values (never a literal "NaN")', () => {
    expect(formatMeasureValue(NaN, "currency")).toBe("");
    expect(formatMeasureValue(Infinity, "number")).toBe("");
    expect(formatMeasureValue(Number(undefined), "percent")).toBe("");
  });
});
