import { advanceISO, formatAmount, formatLocalDate, toEditAmount, todayISO } from "@/lib/format";
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

describe("advanceISO (TBD-275)", () => {
  // The client mirror of backend `date_utils.advance_date`. It exists so
  // promote-to-recurring can send the NEXT occurrence rather than the
  // transaction's own date -- a frontier sitting on the source row's date is
  // matched by generation's idempotency probe and consumed as an instalment.
  it("advances weekly and biweekly by whole days", () => {
    expect(advanceISO("2026-03-05", "weekly")).toBe("2026-03-12");
    expect(advanceISO("2026-03-05", "biweekly")).toBe("2026-03-19");
    // Across a month boundary, so a naive same-month arithmetic is caught.
    expect(advanceISO("2026-03-28", "weekly")).toBe("2026-04-04");
  });

  it("advances monthly, quarterly and yearly", () => {
    expect(advanceISO("2026-03-05", "monthly")).toBe("2026-04-05");
    expect(advanceISO("2026-03-05", "quarterly")).toBe("2026-06-05");
    expect(advanceISO("2026-03-05", "yearly")).toBe("2027-03-05");
    // Year rollover on the monthly arm.
    expect(advanceISO("2026-12-15", "monthly")).toBe("2027-01-15");
  });

  it("CLAMPS a month-end date instead of overflowing into the next month", () => {
    // ⭐ THE fence. `new Date(y, m + 1, 31)` for January silently rolls to
    // March 3 (or March 2 in a leap year), which is a date the series will
    // never land on. `relativedelta` clamps to the last valid day, and this
    // helper must agree.
    expect(advanceISO("2026-01-31", "monthly")).toBe("2026-02-28");
    expect(advanceISO("2028-01-31", "monthly")).toBe("2028-02-29"); // leap
    expect(advanceISO("2026-03-31", "monthly")).toBe("2026-04-30");
    expect(advanceISO("2026-11-30", "quarterly")).toBe("2027-02-28");
    // Feb 29 + 1 year has no counterpart and clamps to Feb 28.
    expect(advanceISO("2028-02-29", "yearly")).toBe("2029-02-28");
  });

  it("returns the input unchanged when it does not parse", () => {
    expect(advanceISO("", "monthly")).toBe("");
    expect(advanceISO("not-a-date", "monthly")).toBe("not-a-date");
  });

  it("treats an unknown frequency as monthly, matching the backend fallback", () => {
    expect(advanceISO("2026-03-05", "fortnightly")).toBe("2026-04-05");
  });
});
