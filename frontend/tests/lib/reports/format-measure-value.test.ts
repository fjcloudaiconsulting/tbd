import { describe, it, expect } from "vitest";

import { formatMeasureValue } from "@/lib/reports/series";
import { formatAmount } from "@/lib/format";

describe("formatMeasureValue", () => {
  it("currency → grouped, 2 decimals, no currency symbol", () => {
    const out = formatMeasureValue(1234.56, "currency");
    // Matches the app's grouped-no-symbol money convention.
    expect(out).toBe(formatAmount(1234.56));
    expect(out).toBe("1,234.56");
    // Currency symbols are deferred to the future multi-currency work.
    expect(out).not.toContain("$");
    expect(out).not.toContain("USD");
  });

  it("percent → one-decimal percentage", () => {
    expect(formatMeasureValue(12.3, "percent")).toBe("12.3%");
    expect(formatMeasureValue(12.34, "percent")).toBe("12.3%");
  });

  it("number → grouped integer, no decimals forced", () => {
    expect(formatMeasureValue(1234, "number")).toBe("1,234");
  });
});
