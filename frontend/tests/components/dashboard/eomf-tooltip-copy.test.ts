import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Regression guard: the end-of-month-forecast tooltip on the Dashboard
 * must accurately reflect the backend computation, which is:
 *
 *   expected_account_balance = current balance + pending deltas in period
 *
 * Recurring activity is NOT factored in (see
 * backend/app/services/account_balance_forecast_service.py docstring).
 *
 * PR #226 reviewer caught a copy drift that claimed "any planned
 * recurring activity" was included. This test reads the page source and
 * asserts the corrected copy stays in place, and that the misleading
 * "recurring activity" phrasing does not return.
 */
describe("Dashboard EOMF tooltip copy", () => {
  const pageSource = readFileSync(
    resolve(__dirname, "../../../app/dashboard/page.tsx"),
    "utf8",
  );

  it("describes the forecast as current balance plus pending only", () => {
    expect(pageSource).toContain(
      "Each account's current balance plus its pending transactions in this billing period.",
    );
  });

  it("explicitly notes that recurring activity is not factored in", () => {
    expect(pageSource).toContain("Recurring activity is not factored in.");
  });

  it("does not claim recurring activity is included in the forecast", () => {
    // Reject the historically-wrong phrasing. We match on the misleading
    // fragment, not the whole sentence, because rewordings should still
    // fail this guard if they imply recurring activity is included.
    expect(pageSource).not.toMatch(/planned recurring activity/i);
    expect(pageSource).not.toMatch(/includes recurring/i);
  });
});
