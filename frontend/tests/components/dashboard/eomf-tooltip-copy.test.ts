import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Regression guard: the end-of-month-forecast tooltip on the Dashboard must
 * accurately reflect the backend computation.
 *
 * ⚠ THE CLAIM THIS GUARD MAKES WAS INVERTED BY TBD-198, AND THIS FILE WAS
 * PINNING THE WRONG SIDE OF IT.
 *
 * Before TBD-198 the backend really did compute
 *
 *     expected_account_balance = current balance + pending deltas in period
 *
 * and `account_balance_forecast_service` met a recurring template only AFTER
 * `generate_due_transactions` had materialised it into a row. PR #226's
 * reviewer caught copy that wrongly claimed recurring activity was included,
 * and this file was written to hold the correction in place.
 *
 * TBD-198 made the claim false in the other direction: the service now carries
 * its own recurring projection (plus the CC and loan payment synthesis it
 * already had), and `expected_month_end_balance` is the last point of a daily
 * walk over all of them. "Recurring activity is not factored in" became a lie
 * on the very same screen -- and the third `it` below, as originally written,
 * FORBADE the true statement (`not.toMatch(/includes recurring/i)`), so the
 * fence would have failed the fix rather than the bug.
 *
 * The guard is therefore re-pointed rather than deleted: same shape, same
 * source-reading approach, opposite polarity. Both historically-wrong
 * phrasings are now rejected, because both have shipped.
 */
describe("Dashboard EOMF tooltip copy", () => {
  const pageSource = readFileSync(
    resolve(__dirname, "../../../app/dashboard/page.tsx"),
    "utf8",
  );

  it("describes the forecast as balance plus everything still expected", () => {
    expect(pageSource).toContain(
      "Each account's current balance plus everything still expected in this billing period: pending transactions, projected card and loan payments, and upcoming recurring activity.",
    );
  });

  it("does not claim recurring activity is excluded (false since TBD-198)", () => {
    // Matched as a fragment, not a whole sentence, so a reworded exclusion
    // still fails: any phrasing that tells the user recurring is left out is
    // now describing a computation the service does not perform.
    expect(pageSource).not.toMatch(/recurring activity is not factored in/i);
    expect(pageSource).not.toMatch(/recurring[^.]{0,40}\bnot\b[^.]{0,40}includ/i);
  });

  it("does not narrow the forecast back to pending-only", () => {
    // The pre-TBD-198 sentence, which named pending as the only addition and
    // therefore left the CC payment, the loan payment and the recurring
    // occurrence unaccounted for on a row the user is asked to reconcile.
    expect(pageSource).not.toContain(
      "current balance plus its pending transactions in this billing period.",
    );
  });
});
