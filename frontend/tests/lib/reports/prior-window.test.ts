/**
 * TBD-383 — the pure surface of `lib/reports/useReportQuery`:
 * `resolvePriorWindow` and `readMeasureValue`.
 *
 * The KPI delta's render-path fences live in
 * `tests/components/reports/widgets/kpi-prior-period.test.tsx`. This file
 * fences the arithmetic and, more importantly, the REASON a window is
 * refused.
 *
 * ⚠ Why the reason is part of the contract. Three of the refusals ("no date
 * filter", "half-open", "relative token") are observationally identical from
 * outside the widget: all three render the value with no delta and fire no
 * second query. So a fence that only asserts "no delta" cannot tell a
 * deliberate refusal from an implementation that reached for an absent
 * `start`/`end`, got `undefined`, and was right by accident. `next_cycle` is
 * the case that matters: the client holds only a token and the absolute
 * window is resolved server-side per request, so the client must not guess —
 * and must be seen to be refusing for THAT reason.
 *
 * ⚠ `today` is INJECTED on every call, never read from the clock. The
 * resolver is wall-clock-free by construction, so none of these cases is a
 * date bomb.
 */
import { readMeasureValue, resolvePriorWindow } from "@/lib/reports/useReportQuery";
import type { Filter } from "@/lib/reports/types";

/** After every fixture window below, so the unclamped path is exercised. */
const TODAY = "2026-12-31";

function between(start: string, end: string): Filter[] {
  return [{ field: "date", op: "between", value: [start, end] }];
}

describe("resolvePriorWindow — bounded windows", () => {
  it("shifts a whole calendar month back by its own length", () => {
    expect(resolvePriorWindow(between("2026-01-01", "2026-01-31"), TODAY)).toEqual({
      prior: ["2025-12-01", "2025-12-31"],
      refusal: null,
    });
  });

  it("shifts a single day back by one day", () => {
    expect(resolvePriorWindow(between("2026-03-17", "2026-03-17"), TODAY)).toEqual({
      prior: ["2026-03-16", "2026-03-16"],
      refusal: null,
    });
  });

  it("maps a whole month to the whole previous month, whatever their lengths (TBD-488)", () => {
    // March (31) -> February (28), not the 31 days ending 2026-02-28.
    expect(resolvePriorWindow(between("2026-03-01", "2026-03-31"), TODAY)).toEqual({
      prior: ["2026-02-01", "2026-02-28"],
      refusal: null,
    });
  });

  it("crosses a leap day without drifting", () => {
    expect(resolvePriorWindow(between("2024-03-01", "2024-03-07"), TODAY)).toEqual({
      prior: ["2024-02-23", "2024-02-29"],
      refusal: null,
    });
  });

  it("crosses a year boundary", () => {
    expect(resolvePriorWindow(between("2026-01-05", "2026-01-11"), TODAY)).toEqual({
      prior: ["2025-12-29", "2026-01-04"],
      refusal: null,
    });
  });

  it("ignores non-date filters when locating the window", () => {
    const filters: Filter[] = [
      { field: "txn_type", op: "in", value: ["expense"] },
      { field: "date", op: "between", value: ["2026-06-01", "2026-06-30"] },
      { field: "status", op: "eq", value: "settled" },
    ];
    expect(resolvePriorWindow(filters, TODAY)).toEqual({
      prior: ["2026-05-01", "2026-05-31"],
      refusal: null,
    });
  });
});

describe("resolvePriorWindow — refusals, each for its own reason", () => {
  it("no date filter at all is `no_date_filter`", () => {
    expect(resolvePriorWindow([], TODAY)).toEqual({
      prior: null,
      refusal: "no_date_filter",
    });
    expect(
      resolvePriorWindow([{ field: "txn_type", op: "in", value: ["expense"] }], TODAY),
    ).toEqual({ prior: null, refusal: "no_date_filter" });
  });

  it("`gte` is `half_open` — there is no length to shift by", () => {
    expect(
      resolvePriorWindow([{ field: "date", op: "gte", value: "2026-01-01" }], TODAY),
    ).toEqual({ prior: null, refusal: "half_open" });
  });

  it("`lte` is `half_open` too", () => {
    expect(
      resolvePriorWindow([{ field: "date", op: "lte", value: "2026-01-31" }], TODAY),
    ).toEqual({ prior: null, refusal: "half_open" });
  });

  it("⚠ a relative token is `relative_token`, NOT `half_open` or `no_date_filter`", () => {
    // The whole point: a client-side implementation reading `start`/`end`
    // would refuse here too, but would report the wrong reason — and would
    // be wrong outright the day a relative token gains a client-visible
    // window. This assertion is what makes the refusal deliberate.
    expect(
      resolvePriorWindow(
        [{ field: "date", op: "relative", value: "next_cycle" }],
        TODAY,
      ),
    ).toEqual({ prior: null, refusal: "relative_token" });
  });

  it("a malformed date is `malformed`, never a silently shifted window", () => {
    expect(resolvePriorWindow(between("not-a-date", "2026-01-31"), TODAY)).toEqual({
      prior: null,
      refusal: "malformed",
    });
    // 2026-02-30 does not exist. `Date.UTC` would roll it to 2026-03-02 and
    // hand back a window nobody asked for.
    expect(resolvePriorWindow(between("2026-02-01", "2026-02-30"), TODAY)).toEqual({
      prior: null,
      refusal: "malformed",
    });
    expect(
      resolvePriorWindow(
        [{ field: "date", op: "between", value: ["2026-01-01"] }],
        TODAY,
      ),
    ).toEqual({ prior: null, refusal: "malformed" });
  });

  it("an inverted range is `inverted`", () => {
    expect(resolvePriorWindow(between("2026-01-31", "2026-01-01"), TODAY)).toEqual({
      prior: null,
      refusal: "inverted",
    });
  });
});

describe("resolvePriorWindow — a window that has not finished yet is clamped to the days it has reached", () => {
  // ⚠⚠ B1. `draft.ts` seeds every new report with `this_month`, and
  // `buildPresetRanges` freezes that as the WHOLE calendar month
  // (`startOfMonth(now)..endOfMonth(now)`), not month-to-date. Without the
  // clamp, 2 days of September data were compared against 30 complete days of
  // August and the widget rendered roughly "-92%" for a user whose spending
  // had not changed — on the default authoring path, for most of every month,
  // under a label asserting comparability. That is worse than the dead
  // feature it replaces: absent became actively false.
  //
  // ⚠ Refusing a future-ending window instead would kill the delta on the
  // default preset entirely.
  //
  // ⚠ NOT complete-to-complete, and this block does not claim to be. `today`
  // counts as a whole day on both sides, so on day N it compares N-1 complete
  // days plus a partial today against N complete ones: unchanged spending
  // still reads about -29% on day 2 at 10:00 and about -4% by day 15.
  // `min(end, today - 1)` would be exact, and is not used because it refuses
  // on the 1st, when no complete day exists yet.
  it("compares September 1-2 against August 1-2, not against all of August", () => {
    expect(
      resolvePriorWindow(between("2026-09-01", "2026-09-30"), "2026-09-02"),
    ).toEqual({ prior: ["2026-08-01", "2026-08-02"], refusal: null });
  });

  it("on the FIRST of the month compares one day against one day", () => {
    // The worst case before the clamp: 1 day of data vs 30, i.e. ~-100%.
    expect(
      resolvePriorWindow(between("2026-09-01", "2026-09-30"), "2026-09-01"),
    ).toEqual({ prior: ["2026-08-01", "2026-08-01"], refusal: null });
  });

  it("leaves a FULLY PAST window untouched (the clamp is not a rewrite)", () => {
    expect(
      resolvePriorWindow(between("2026-01-01", "2026-01-31"), "2026-09-02"),
    ).toEqual({ prior: ["2025-12-01", "2025-12-31"], refusal: null });
  });

  it("leaves a window ENDING TODAY untouched (`ytd`, `last_12_months`)", () => {
    // Those two presets end at `now`, so they have no partial-window problem.
    // Asserted by equivalence rather than by a hand-computed window, so the
    // case cannot be made vacuous by a wrong constant.
    const ytd = between("2026-01-01", "2026-09-02");
    expect(resolvePriorWindow(ytd, "2026-09-02")).toEqual(
      resolvePriorWindow(ytd, "2027-06-01"),
    );
    expect(resolvePriorWindow(ytd, "2026-09-02").refusal).toBeNull();
  });

  it("refuses a WHOLLY future window — nothing has elapsed to compare", () => {
    expect(
      resolvePriorWindow(between("2027-01-01", "2027-01-31"), "2026-09-02"),
    ).toEqual({ prior: null, refusal: "future_window" });
  });

  it("refuses when `today` itself is unparseable rather than silently not clamping", () => {
    expect(
      resolvePriorWindow(between("2026-09-01", "2026-09-30"), "not-a-date"),
    ).toEqual({ prior: null, refusal: "malformed" });
  });
});

describe("resolvePriorWindow — whole months compare against whole months (TBD-488)", () => {
  // B2, ruled 2026-11-08. Personal cashflow repeats by day of MONTH (rent,
  // salary, subscriptions on fixed days, mostly the 1st), so a window made of
  // whole calendar months shifts back by that many calendar months, both ends.
  // The old same-length rule mapped February to 2026-01-04..01-31 and dropped
  // January's rent. Any other window keeps the same-length slide.
  it("a fully-past February maps to all of January", () => {
    expect(
      resolvePriorWindow(between("2026-02-01", "2026-02-28"), "2026-09-02"),
    ).toEqual({ prior: ["2026-01-01", "2026-01-31"], refusal: null });
  });

  it("a fully-past March maps to a leap February", () => {
    expect(
      resolvePriorWindow(between("2024-03-01", "2024-03-31"), "2024-09-02"),
    ).toEqual({ prior: ["2024-02-01", "2024-02-29"], refusal: null });
  });

  it("this month on the 30th of March compares against February 1-28, not into March", () => {
    expect(
      resolvePriorWindow(between("2026-03-01", "2026-03-31"), "2026-03-30"),
    ).toEqual({ prior: ["2026-02-01", "2026-02-28"], refusal: null });
  });

  it("a whole quarter maps to the previous quarter", () => {
    expect(
      resolvePriorWindow(between("2026-04-01", "2026-06-30"), TODAY),
    ).toEqual({ prior: ["2026-01-01", "2026-03-31"], refusal: null });
  });

  it("a whole year in progress compares the same elapsed part of last year", () => {
    expect(
      resolvePriorWindow(between("2026-01-01", "2026-12-31"), "2026-11-08"),
    ).toEqual({ prior: ["2025-01-01", "2025-11-08"], refusal: null });
  });

  it("a quarter cut short on a month's last day compares through the earlier month's last day", () => {
    // Nov 30 is November's end, so the prior end is August's end, not Aug 30.
    expect(
      resolvePriorWindow(between("2026-10-01", "2026-12-31"), "2026-11-30"),
    ).toEqual({ prior: ["2026-07-01", "2026-08-31"], refusal: null });
  });

  it("a range that is not whole months keeps the same-length slide", () => {
    // Starts on the 2nd: not a whole month, so 30 days back.
    expect(
      resolvePriorWindow(between("2026-03-02", "2026-03-31"), TODAY),
    ).toEqual({ prior: ["2026-01-31", "2026-03-01"], refusal: null });
  });
});

describe("readMeasureValue", () => {
  // ⚠ Extracted so the KPI's value and its comparison value can never coerce
  // differently. ZERO is the case that matters and was the case not covered:
  // a helper returning `null` for `0` suppresses a legitimate headline number
  // (the widget renders an em-dash instead) and silently changes which side
  // of the division guard a comparison lands on.
  it("returns 0 for a numeric zero, NOT null", () => {
    expect(readMeasureValue({ value: 0 })).toBe(0);
  });

  it("returns 0 for the STRING zero its contract names, NOT null", () => {
    expect(readMeasureValue({ value: "0" })).toBe(0);
  });

  it("coerces a numeric string", () => {
    expect(readMeasureValue({ value: "1234.56" })).toBe(1234.56);
  });

  it("returns null for a missing row, a null value, and a non-numeric string", () => {
    expect(readMeasureValue(undefined)).toBeNull();
    expect(readMeasureValue({ value: null })).toBeNull();
    expect(readMeasureValue({ value: "not a number" })).toBeNull();
    expect(readMeasureValue({})).toBeNull();
  });
});
