import { formatAmount, formatLocalDate, toEditAmount, todayISO } from "@/lib/format";


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
