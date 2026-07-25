"use client";

/**
 * LoanPayoffTile — "Loan payoff" dashboard tile (Group C item 7). A glanceable
 * SUMMARY of each loan's payoff status + next payment. Reads activeAccounts
 * (loan metrics) AND accountMonthEndForecast (Slice-2 loan_payments) from
 * DashboardDataProvider, joined by account_id, one row per loan.
 *
 * Deliberately NOT a transplant of the accounts-page detail card and NOT a
 * restatement of `dash_balances_by_type`: no balance, no rate/term/matures/
 * interest. It earns its place via payoff STATUS + NEXT PAYMENT only. The one
 * expressive (colored) moment per row is the status badge; the next-payment
 * line is quiet muted text so the glance surface stays calm across N rows.
 *
 * Status classification (state -> tone) is shared with the accounts card via
 * `loanPayoffStatus` (lib/loan.ts) so the two surfaces can't drift; the labels
 * here are intentionally terser than the card's (per-surface copy).
 */
import { useMemo } from "react";
import Link from "next/link";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { formatAmount } from "@/lib/format";
import { loanPayoffStatus, type LoanPayoffState } from "@/lib/loan";
import { badgeForTone, card, cardHeader, cardTitle } from "@/lib/styles";

/** Terse glance labels (the accounts card carries the verbose copy). */
const LABEL: Record<LoanPayoffState, string> = {
  on_track: "On track",
  interest_only: "Interest only",
  paid_off: "Paid off",
  setup: "Needs setup",
};

/** Attention-first: action items (not amortizing; incomplete data) above
 *  reassurance (on track) above done (paid off). */
const STATE_ORDER: Record<LoanPayoffState, number> = {
  interest_only: 0,
  setup: 1,
  on_track: 2,
  paid_off: 3,
};

const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent";

export default function LoanPayoffTile() {
  const { activeAccounts, accountMonthEndForecast } = useDashboard();

  const nextPaymentByAccount = useMemo(() => {
    const map: Record<number, { amount: string; date: string }> = {};
    for (const row of accountMonthEndForecast?.accounts ?? []) {
      const first = row.loan_payments?.[0];
      if (first) map[row.account_id] = first;
    }
    return map;
  }, [accountMonthEndForecast]);

  const loans = useMemo(() => {
    return activeAccounts
      .filter((a) => a.account_type_slug === "loan")
      .map((a) => ({ account: a, status: loanPayoffStatus(a.loan) }))
      .sort((x, y) => {
        const byState = STATE_ORDER[x.status.state] - STATE_ORDER[y.status.state];
        if (byState !== 0) return byState;
        // Tie-break: soonest next payment first (missing dates sort last), then name.
        const dx = nextPaymentByAccount[x.account.id]?.date ?? "￿";
        const dy = nextPaymentByAccount[y.account.id]?.date ?? "￿";
        if (dx !== dy) return dx < dy ? -1 : 1;
        return x.account.name.localeCompare(y.account.name);
      });
  }, [activeAccounts, nextPaymentByAccount]);

  return (
    <div data-testid="loan-payoff-tile" className={`${card} flex flex-col overflow-hidden`}>
      <div className={`flex items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Loan payoff</h2>
        <Link
          href="/accounts"
          className={`rounded-sm text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary ${FOCUS_RING}`}
        >
          Accounts
        </Link>
      </div>

      {loans.length === 0 ? (
        <div className="px-5 py-6 text-center text-sm text-text-muted">
          No loans yet.{" "}
          <Link
            href="/accounts"
            className={`rounded-sm text-text-primary underline underline-offset-2 hover:text-text-secondary ${FOCUS_RING}`}
          >
            Add one
          </Link>
        </div>
      ) : (
        <div className="divide-y divide-border-subtle">
          {loans.map(({ account, status }) => {
            const next = nextPaymentByAccount[account.id];
            return (
              <div
                key={account.id}
                data-testid="loan-payoff-row"
                className="flex flex-col gap-1 px-4 py-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-text-primary">
                    {account.name}
                  </span>
                  <span className={`shrink-0 ${badgeForTone(status.tone)}`}>
                    {LABEL[status.state]}
                  </span>
                </div>
                {next && (
                  <span className="text-xs text-text-muted">
                    Next payment {formatAmount(next.amount)} {account.currency} on {next.date}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
