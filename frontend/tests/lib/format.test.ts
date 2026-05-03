import { formatAmount, formatLocalDate, toEditAmount, todayISO } from "@/lib/format";
import { equalsAmount } from "@/lib/format";


describe("format utilities", () => {
  it("normalizes amounts for inline-edit seeding to a clean two-decimal string", () => {
    // L3.9 — defense for the case where the JSON-string Decimal arrives as
    // an IEEE 754 number (19.989999...): the inline-edit input must seed
    // "19.99" exactly so a save-without-touch round-trips cleanly.
    expect(toEditAmount("19.99")).toBe("19.99");
    expect(toEditAmount(19.989999771118164)).toBe("19.99");
    expect(toEditAmount(0)).toBe("0.00");
    expect(toEditAmount("100")).toBe("100.00");
  });

  it("formats numeric strings and negative values with two decimals", () => {
    const formatter = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });

    expect(formatAmount("1234.5")).toBe(formatter.format(1234.5));
    expect(formatAmount(-9)).toBe(formatter.format(-9));
  });

  it("formats local dates as YYYY-MM-DD", () => {
    expect(formatLocalDate(new Date(2026, 3, 24))).toBe("2026-04-24");
  });

  it("uses the current local date for todayISO", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 3, 24, 15, 30, 0));

    expect(todayISO()).toBe("2026-04-24");

    vi.useRealTimers();
  });
});

describe("equalsAmount", () => {
  it("returns true for normalized equal strings", () => {
    expect(equalsAmount("100.00", "100")).toBe(true);
    expect(equalsAmount("100.0", "100.00")).toBe(true);
    expect(equalsAmount("0", "0.00")).toBe(true);
    expect(equalsAmount("1.50", "1.5")).toBe(true);
  });

  it("returns false for unequal strings", () => {
    expect(equalsAmount("100.01", "100.00")).toBe(false);
    expect(equalsAmount("100", "1000")).toBe(false);
  });

  it("handles negative values", () => {
    expect(equalsAmount("-100.00", "-100")).toBe(true);
    expect(equalsAmount("-100", "100")).toBe(false);
  });

  it("does not use float comparison (0.1 + 0.2 case)", () => {
    expect(equalsAmount("0.30", "0.30")).toBe(true);
  });
});
