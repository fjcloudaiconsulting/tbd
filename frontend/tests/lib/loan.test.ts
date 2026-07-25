import { describe, expect, test } from "vitest";

import { loanPayoffStatus } from "@/lib/loan";
import { badgeForTone } from "@/lib/styles";
import type { LoanMetrics } from "@/lib/types";

// Loose fixture: LoanMetrics amount fields are Pydantic-Decimal strings at
// runtime; only `status` matters for classification.
function metrics(status: LoanMetrics["status"]): LoanMetrics {
  return {
    expected_monthly_payment: "100.00",
    maturation_date: "2030-01-01",
    total_interest: "500.00",
    projected_payoff_date: status === "interest_only" ? null : "2030-01-01",
    projected_payoff_months: status === "interest_only" ? null : 60,
    status,
  };
}

describe("loanPayoffStatus", () => {
  test("on_track -> success", () => {
    expect(loanPayoffStatus(metrics("on_track"))).toEqual({ state: "on_track", tone: "success" });
  });
  test("paid_off -> neutral", () => {
    expect(loanPayoffStatus(metrics("paid_off"))).toEqual({ state: "paid_off", tone: "neutral" });
  });
  test("interest_only -> warning", () => {
    expect(loanPayoffStatus(metrics("interest_only"))).toEqual({ state: "interest_only", tone: "warning" });
  });
  test("null loan -> setup/info", () => {
    expect(loanPayoffStatus(null)).toEqual({ state: "setup", tone: "info" });
  });
  test("undefined loan -> setup/info", () => {
    expect(loanPayoffStatus(undefined)).toEqual({ state: "setup", tone: "info" });
  });

  // The tone the classifier returns must resolve to a real badge token, so the
  // accounts card and the dashboard tile can't drift on which colour a state gets.
  test("every returned tone resolves to a badge class", () => {
    for (const s of ["on_track", "paid_off", "interest_only"] as const) {
      expect(badgeForTone(loanPayoffStatus(metrics(s)).tone)).toBeTruthy();
    }
    expect(badgeForTone(loanPayoffStatus(null).tone)).toBeTruthy();
  });
});
