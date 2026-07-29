/**
 * TBD-242 — the single frontend definition of "the current billing period".
 *
 * Two separate questions live here and the tests keep them apart on purpose,
 * because conflating them is the defect this ticket exists to fix:
 *
 *   * `periodStatus()` CLASSIFIES a row against a clock. It mirrors
 *     `billing_service.period_status()` (backend/app/services/billing_service.py:1414)
 *     branch for branch, in the same normative order.
 *   * `selectCurrentPeriod()` SELECTS the row a screen shows. It mirrors
 *     `billing_service.get_current_period()` (…:76-96) — newest OPEN row —
 *     and is deliberately CLOCK-FREE.
 *
 * The backend classifier's docstring says it "classifies rows, it does not
 * SELECT one" and hands selection to this ticket. These two functions are
 * that split, made explicit.
 */
import { describe, expect, it } from "vitest";

import {
  isOpenPeriod,
  periodStatus,
  selectCurrentPeriod,
  selectCurrentPeriodIndex,
} from "@/lib/billingPeriodStatus";

const p = (id: number, start_date: string, end_date: string | null = null) => ({
  id,
  start_date,
  end_date,
});

const TODAY = "2026-07-29";

describe("periodStatus — the five-branch partition, in normative order", () => {
  it("1. `invalid` wins over every later branch (end_date < start_date)", () => {
    // Inverted AND open-ended is impossible, but inverted AND calendar-
    // containing is not: without branch 1 first this row matches both
    // `upcoming` and `past`, breaking the partition.
    expect(periodStatus(p(1, "2026-07-20", "2026-07-10"), TODAY)).toBe("invalid");
    expect(periodStatus(p(2, "2999-01-01", "1999-01-01"), TODAY)).toBe("invalid");
  });

  it("2. `open` — end_date IS NULL — regardless of where start_date sits", () => {
    expect(periodStatus(p(3, "2026-07-01", null), TODAY)).toBe("open");
    // A LAPSED open row: started long ago, still open. Still `open`, never `past`.
    expect(periodStatus(p(4, "2020-01-01", null), TODAY)).toBe("open");
    // A FUTURE open row is `open`, not `upcoming` — branch 2 precedes branch 3.
    expect(periodStatus(p(5, "2999-01-01", null), TODAY)).toBe("open");
  });

  it("3. `upcoming` — start_date > today", () => {
    expect(periodStatus(p(6, "2026-07-30", "2026-08-29"), TODAY)).toBe("upcoming");
  });

  it("4. `current_by_calendar` — start_date <= today <= end_date", () => {
    expect(periodStatus(p(7, "2026-07-01", "2026-07-31"), TODAY)).toBe(
      "current_by_calendar",
    );
    // Both bounds are INCLUSIVE.
    expect(periodStatus(p(8, TODAY, TODAY), TODAY)).toBe("current_by_calendar");
  });

  it("5. `past` — end_date < today", () => {
    expect(periodStatus(p(9, "2026-06-01", "2026-06-30"), TODAY)).toBe("past");
    // The day after end_date is already `past`.
    expect(periodStatus(p(10, "2026-06-01", "2026-07-28"), TODAY)).toBe("past");
  });

  it("partitions any roster totally — every row gets exactly one status", () => {
    const rows = [
      p(1, "2026-07-20", "2026-07-10"),
      p(2, "2020-01-01", null),
      p(3, "2026-07-30", "2026-08-29"),
      p(4, "2026-07-01", "2026-07-31"),
      p(5, "2026-06-01", "2026-06-30"),
    ];
    const seen = rows.map((r) => periodStatus(r, TODAY));
    expect(seen).toEqual([
      "invalid",
      "open",
      "upcoming",
      "current_by_calendar",
      "past",
    ]);
  });
});

describe("selectCurrentPeriod — mirrors get_current_period, newest OPEN row", () => {
  it("picks the only open row on a healthy roster", () => {
    const periods = [
      p(1, "2026-05-01", "2026-05-31"),
      p(2, "2026-06-01", "2026-06-30"),
      p(3, "2026-07-01", null),
    ];
    expect(selectCurrentPeriod(periods)?.id).toBe(3);
  });

  it("is INDEPENDENT of list order — newest open row, not first-in-list", () => {
    // The endpoint's ordering is not a contract the callers should lean on.
    const newestFirst = [p(3, "2026-07-01", null), p(1, "2026-05-01", "2026-05-31")];
    const oldestFirst = [p(1, "2026-05-01", "2026-05-31"), p(3, "2026-07-01", null)];
    expect(selectCurrentPeriod(newestFirst)?.id).toBe(3);
    expect(selectCurrentPeriod(oldestFirst)?.id).toBe(3);
  });

  it("on a DUPLICATE-OPEN roster picks max start_date, matching the backend", () => {
    // get_current_period ORDER BY start_date DESC LIMIT 1. A `.find()` would
    // take the first in list order and disagree with whatever the backend
    // writes to. This is the bug the roster page's `duplicate_open` marker
    // warns about: "Different screens can pick different ones as the current
    // period."
    const periods = [
      p(1, "2026-06-01", null),
      p(2, "2026-07-01", null),
      p(3, "2026-05-01", null),
    ];
    expect(selectCurrentPeriod(periods)?.id).toBe(2);
  });

  it("on a LAPSED roster still picks the open row, NOT the calendar-containing stub", () => {
    // This is the divergence TBD-242 must resolve, and it resolves toward the
    // backend. `resolve_period` falls back to `get_current_period` for any
    // write that omits `period_start`, so if the frontend displayed the stub
    // while the backend wrote to the open row, the user's edit would land on a
    // period they are not looking at.
    const openRow = p(1, "2026-01-01", null);
    const containingStub = p(2, "2026-07-01", "2026-07-31");
    expect(selectCurrentPeriod([openRow, containingStub])?.id).toBe(1);
    expect(periodStatus(containingStub, TODAY)).toBe("current_by_calendar");
    expect(periodStatus(openRow, TODAY)).toBe("open");
  });

  it("breaks a same-start_date tie on id, independent of array order", () => {
    // Unreachable via the DB (uq_billing_period_org_start), but the input is
    // an untrusted array. Without the tie-break the answer would depend on
    // array order, which is the defect this module exists to remove.
    const a = p(7, "2026-07-01", null);
    const b = p(9, "2026-07-01", null);
    expect(selectCurrentPeriod([a, b])?.id).toBe(9);
    expect(selectCurrentPeriod([b, a])?.id).toBe(9);
  });

  it("returns null on an empty roster", () => {
    expect(selectCurrentPeriod([])).toBeNull();
  });

  it("returns null when NO row is open, rather than falling through to periods[0]", () => {
    // The `no_open` anomaly. Today ~7 sites do `periods.find(p => !p.end_date)`
    // then `?? periods[0]`, silently showing the NEWEST row as if it were
    // current. Returning null forces the caller to handle it.
    const periods = [p(1, "2026-06-01", "2026-06-30"), p(2, "2026-07-01", "2026-07-31")];
    expect(selectCurrentPeriod(periods)).toBeNull();
  });

  it("never consults a clock", () => {
    // Guards against a future refactor quietly reintroducing calendar
    // containment into SELECTION, which would desync from the backend.
    const periods = [p(1, "2999-01-01", null)];
    expect(selectCurrentPeriod(periods)?.id).toBe(1);
  });
});

describe("selectCurrentPeriodIndex — the most-used export, and the one most easily reverted", () => {
  // ⚠ These exist because the migration replaced eight `findIndex(p =>
  // p.end_date === null)` call sites with this function, and NOTHING in the
  // suite failed when it was mutated back to exactly that expression. A
  // helper whose regression is invisible is not a fix, it is a rename.

  it("returns the index of the newest open row, NOT the first one in the list", () => {
    // The mutation `periods.findIndex(isOpenPeriod)` returns 0 here. This is
    // the pre-refactor rule, and it is the whole bug: on a duplicate-open
    // roster that is not newest-first, the screen and the backend disagree
    // about which period a write lands in.
    const periods = [
      p(1, "2026-06-01", null),
      p(2, "2026-07-01", null),
      p(3, "2026-05-01", null),
    ];
    expect(selectCurrentPeriodIndex(periods)).toBe(1);
  });

  it("returns an INDEX INTO THE GIVEN ARRAY, matching selectCurrentPeriod", () => {
    const periods = [
      p(1, "2026-05-01", "2026-05-31"),
      p(2, "2026-06-01", "2026-06-30"),
      p(3, "2026-07-01", null),
    ];
    const idx = selectCurrentPeriodIndex(periods);
    expect(idx).toBe(2);
    expect(periods[idx]).toBe(selectCurrentPeriod(periods));
  });

  it("returns -1 when no row is open, so callers can tell 'none' from index 0", () => {
    // Every caller guards with `idx >= 0`. If this returned 0 instead, the
    // dashboard/budgets/forecasts would silently present the FIRST row as the
    // current period on a `no_open` roster.
    const periods = [
      p(1, "2026-06-01", "2026-06-30"),
      p(2, "2026-07-01", "2026-07-31"),
    ];
    expect(selectCurrentPeriodIndex(periods)).toBe(-1);
  });

  it("returns -1 on an empty roster", () => {
    expect(selectCurrentPeriodIndex([])).toBe(-1);
  });

  it("agrees with selectCurrentPeriod on every shape", () => {
    const rosters = [
      [],
      [p(1, "2026-07-01", null)],
      [p(1, "2026-06-01", "2026-06-30")],
      [p(1, "2026-06-01", null), p(2, "2026-07-01", null)],
      [p(2, "2026-07-01", null), p(1, "2026-06-01", null)],
      [p(1, "2026-01-01", null), p(2, "2026-07-01", "2026-07-31")],
    ];
    for (const roster of rosters) {
      const idx = selectCurrentPeriodIndex(roster);
      const picked = selectCurrentPeriod(roster);
      expect(idx === -1 ? null : roster[idx]).toBe(picked);
    }
  });
});

describe("isOpenPeriod", () => {
  it("is true only for a null end_date", () => {
    expect(isOpenPeriod(p(1, "2026-07-01", null))).toBe(true);
    expect(isOpenPeriod(p(2, "2026-07-01", "2026-07-31"))).toBe(false);
  });
});
