/**
 * TBD-242 — the TypeScript side of the period-status cross-language contract.
 *
 * `periodStatus()` is a port of `billing_service.period_status`. This test and
 * its pytest twin (`backend/tests/test_period_status_frontend_contract.py`)
 * read the SAME generated fixture, so a change on either side turns the other
 * side's suite red.
 *
 * ⚠ The fixture is GENERATED (`backend/scripts/gen_period_status_vectors.py`).
 * Never hand-edit it to make this test pass — a changed vector means the
 * Python classifier changed, and the port must be changed to match, not the
 * evidence.
 */
import { describe, expect, it } from "vitest";

import vectors from "@/tests/fixtures/period-status-vectors.json";
import { periodStatus, type PeriodStatus } from "@/lib/billingPeriodStatus";

describe("periodStatus matches the backend contract fixture", () => {
  it("reproduces every generated vector", () => {
    const mismatches = vectors.vectors
      .map((v) => {
        const actual = periodStatus(
          { start_date: v.start_date, end_date: v.end_date },
          vectors.today,
        );
        return actual === v.status
          ? null
          : `(${v.start_date}, ${v.end_date}) -> fixture ${v.status}, ts ${actual}`;
      })
      .filter((m): m is string => m !== null);

    expect(mismatches).toEqual([]);
  });

  it("covers every status the backend Literal declares", () => {
    // Catches a SIXTH branch added in Python: the fixture's `statuses` comes
    // from `get_args(PeriodStatus)`, so a new member lands here first.
    const covered = new Set(vectors.vectors.map((v) => v.status));
    for (const s of vectors.statuses) {
      expect(covered.has(s)).toBe(true);
    }
  });

  it("keeps the TS union exhaustive over the backend's members", () => {
    // ⚠ The previous version of this test was a TAUTOLOGY: it cast
    // `vectors.statuses` to the union and asserted `x.length === x.length`.
    // It could not fail, and its comment claimed a compile-time guarantee it
    // did not have — `vectors.statuses` infers as `string[]` from the JSON
    // import (no literal types), and `as PeriodStatus[]` is an assertion, not
    // a check. A six-member fixture compiled clean and the test stayed green.
    //
    // This version is real in BOTH directions. The Record is a genuine
    // compile-time exhaustiveness check (a missing key or an extra key is a
    // tsc error), and the runtime comparison catches Python-side drift.
    const TS_MEMBERS: Record<PeriodStatus, true> = {
      invalid: true,
      open: true,
      upcoming: true,
      current_by_calendar: true,
      past: true,
    };
    expect([...vectors.statuses].sort()).toEqual(Object.keys(TS_MEMBERS).sort());
  });
});
