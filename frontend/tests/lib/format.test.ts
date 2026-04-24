import { formatAmount, formatLocalDate, todayISO } from "@/lib/format";


describe("format utilities", () => {
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
