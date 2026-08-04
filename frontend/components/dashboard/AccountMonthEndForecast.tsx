"use client";

import Link from "next/link";
import { TriangleAlert } from "lucide-react";

import { badgeError, btnLink, card, cardHeader, cardTitle } from "@/lib/styles";
import { formatAmount } from "@/lib/format";

export interface AccountMonthEndForecastTotal {
  currency: string;
  balance: string;
  pending_delta: string;
  expected_month_end_balance: string;
}

export interface AccountMonthEndForecastRow {
  account_id: number;
  account_name: string;
  currency: string;
  is_default: boolean;
  account_type_slug: string | null;
  balance: string;
  pending_delta: string;
  expected_month_end_balance: string;
  // Slice 3: synthesized credit-card payment(s) projected in this period.
  cc_payments?: { amount: string; date: string }[];
  // Loan V1 Slice 2: synthesized loan payment(s) projected in this period.
  loan_payments?: { amount: string; date: string }[];
  // TBD-198: recurring occurrences PROJECTED into this period but not yet
  // materialised by the generator. `amount` is SIGNED (income positive,
  // expense negative), unlike cc_payments / loan_payments which are always
  // outflows carrying a magnitude.
  recurring_lines?: { amount: string; date: string }[];
  // TBD-198: the end-of-day projected balance for every day of the remaining
  // window. Present so the last point is verifiably the same number as
  // `expected_month_end_balance`; this component renders the RUNS, not the
  // series, so nothing here iterates it.
  daily_balances?: { date: string; balance: string }[];
  // TBD-198: contiguous below-zero intervals, already filtered to the
  // strictly-future ones by the backend. One entry per RUN, never per day.
  risk_days?: LowBalanceRun[];
}

export interface LowBalanceRun {
  from: string;
  through: string;
  lowest_balance: string;
  lowest_on: string;
}

export interface AccountMonthEndForecastResponse {
  period_start: string;
  // TBD-198: first day of `daily_balances` (= max(period_start, today)).
  // Optional here because this widget does not consume it; it is on the wire
  // so a client can tell day 0's re-booked overdue deltas from one day's
  // activity.
  series_start?: string;
  period_end: string;
  totals: AccountMonthEndForecastTotal[];
  accounts: AccountMonthEndForecastRow[];
}

export interface AccountMonthEndForecastProps {
  forecast: AccountMonthEndForecastResponse | null;
  isCurrentPeriod: boolean;
  onJumpToCurrent?: () => void;
  hasAnyAccounts: boolean;
  // True when the most recent fetch attempt failed. Distinguishes "still
  // loading" (forecast null AND no error) from "load failed" (forecast
  // null AND error true). Without this, a 500 from the endpoint would
  // render the same "Loading…" placeholder forever.
  hasError?: boolean;
}

export default function AccountMonthEndForecast({
  forecast,
  isCurrentPeriod,
  onJumpToCurrent,
  hasAnyAccounts,
  hasError = false,
}: AccountMonthEndForecastProps) {
  // No accounts: page-level empty state owns this surface; render nothing
  // regardless of period. Runs BEFORE the period check so an empty org
  // viewing a past/future period doesn't see a neutral month-end card it
  // can never use.
  if (!hasAnyAccounts) return null;

  // Past or future selected period: the stored balance is "now", not
  // historical or future, so projecting it into another period would
  // mislead. Spec mandates a small neutral state instead.
  if (!isCurrentPeriod) {
    return (
      <section className={`${card} p-5`} data-testid="account-month-end-forecast">
        <header className={`mb-2 flex items-center justify-between ${cardHeader}`}>
          <h2 className={cardTitle}>Forecast</h2>
        </header>
        <p className="text-sm text-text-muted">
          Month-end balance forecast is only available for the current period.
        </p>
        {onJumpToCurrent && (
          <div className="mt-3">
            <button
              type="button"
              onClick={onJumpToCurrent}
              className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
            >
              Today
            </button>
          </div>
        )}
      </section>
    );
  }

  if (hasError) {
    return (
      <section className={`${card} p-5`} data-testid="account-month-end-forecast">
        <header className={`mb-2 flex items-center justify-between ${cardHeader}`}>
          <h2 className={cardTitle}>Forecast</h2>
        </header>
        <p className="text-sm text-text-muted">
          Couldn&apos;t load account forecast. Try again later.
        </p>
      </section>
    );
  }

  if (!forecast) {
    return (
      <section className={`${card} p-5`} data-testid="account-month-end-forecast">
        <header className={`mb-2 flex items-center justify-between ${cardHeader}`}>
          <h2 className={cardTitle}>Forecast</h2>
        </header>
        <p className="text-sm text-text-muted">Loading…</p>
      </section>
    );
  }

  const totals = forecast.totals;
  const rows = forecast.accounts;

  return (
    <section className={`${card} p-5`} data-testid="account-month-end-forecast">
      {/* Header consolidated: the eyebrow already names the card
          ("Expected month-end balance"), so the explicit "Forecast"
          card title and the duplicate "Includes pending items"
          supporting line are dropped. A single descriptive line under
          the hero replaces both. */}
      {totals.length > 0 && (
        <div className="mb-4 space-y-1">
          {/* h2 (not p) so the page outline (h1, h2, h2 ...) stays
              consistent with the loading / error / non-current-period
              branches that render <h2>Forecast</h2>. Visual styling
              matches the eyebrow tokens unchanged. */}
          <h2 className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">
            Expected month-end balance
          </h2>
          <div className="space-y-0.5">
            {totals.map((t) => (
              <p
                key={t.currency}
                className="text-2xl font-semibold tabular-nums text-text-primary"
              >
                {formatAmount(t.expected_month_end_balance)}{" "}
                <span className="text-xs font-normal text-text-muted">{t.currency}</span>
              </p>
            ))}
          </div>
          {/* TBD-198. This caption used to read "Current balance plus pending
              items in this period", which was literally true when the number
              was `balance + pending_delta`. It is now the sum of the daily
              walk, which also carries projected card and loan payments and
              upcoming recurring occurrences — so the old caption named three
              of the numbers on screen and silently excluded the rest, and a
              row could show Balance 1000, "-200 pending" and a 450 forecast
              with nothing accounting for the difference. */}
          <p className="text-xs text-text-muted">
            Current balance plus everything still expected in this period.
          </p>
        </div>
      )}

      <div className="overflow-hidden rounded-md border border-border-subtle">
        {/* Account left-aligned (the row anchor); Balance and End of
            month forecast right-aligned per the spec's "currency
            values stay tabular and right-aligned" rule. The pending
            subtext under EOMF inherits right alignment from its
            parent column wrapper. */}
        <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,2fr)_minmax(0,3fr)] items-center gap-x-4 border-b border-border-subtle bg-surface-overlay px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
          <span>Account</span>
          <span className="text-right">Balance</span>
          <span className="text-right">End of month forecast</span>
        </div>
        <div className="divide-y divide-border-subtle">
          {rows.map((row) => {
            const pendingNum = Number(row.pending_delta);
            const showPending = pendingNum !== 0;
            const sign = pendingNum > 0 ? "+" : "-";
            const pendingMagnitude = formatAmount(Math.abs(pendingNum));
            const pendingCurrencySymbol = currencySymbol(row.currency);
            const riskDays = row.risk_days ?? [];
            return (
              <div
                key={row.account_id}
                className="grid grid-cols-[minmax(0,2fr)_minmax(0,2fr)_minmax(0,3fr)] items-center gap-x-4 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm text-text-primary">
                    <span className="truncate">{row.account_name}</span>
                    {row.is_default && (
                      <span className="rounded border border-border px-1.5 py-0.5 text-[9px] font-semibold text-text-secondary">
                        DEFAULT
                      </span>
                    )}
                    {/* TBD-198. `badgeError` DIRECTLY, not via badgeForTone():
                        the BadgeTone union has no danger member and widening
                        it for one caller is scope this ticket does not need.
                        Colour is never the only signal (DESIGN.md, PRODUCT.md,
                        WCAG 2.2 AA) — the icon is aria-hidden and the visible
                        "Low balance" text carries the meaning, with an sr-only
                        prefix so the chip is not heard as another metadata
                        value on the row. Same construction as MarkerChip on
                        /settings/organization/periods. */}
                    {riskDays.length > 0 && (
                      <span
                        // `shrink-0`: this sits in a flex row beside a
                        // `truncate` account name and the DEFAULT chip, inside
                        // a `minmax(0,2fr)` grid column. Without it a long
                        // account name squeezes the badge and "Low balance"
                        // wraps or clips.
                        className={`${badgeError} shrink-0`}
                        data-testid={`low-balance-badge-${row.account_id}`}
                      >
                        <span className="sr-only">Warning: </span>
                        <TriangleAlert className="h-3 w-3" aria-hidden="true" />
                        Low balance
                      </span>
                    )}
                  </p>
                </div>
                <p className="text-right text-sm tabular-nums text-text-secondary">
                  {formatAmount(row.balance)}{" "}
                  <span className="text-[10px] text-text-muted">{row.currency}</span>
                </p>
                <div className="text-right">
                  <p className="text-sm font-medium tabular-nums text-text-primary">
                    {formatAmount(row.expected_month_end_balance)}
                  </p>
                  {showPending && (
                    <p className="text-[10px] tabular-nums text-text-muted">
                      Includes {sign}
                      {pendingCurrencySymbol}
                      {pendingMagnitude} pending
                    </p>
                  )}
                  {/* TBD-198. The dated sub-line, in the SAME visual slot the
                      CC/loan payment lines occupy — quiet by default, and
                      absent entirely when there is nothing to say. One line
                      per RUN: a line per day would turn a fortnight's
                      overdraft into fourteen identical rows. */}
                  {riskDays.map((r) => (
                    <p
                      key={`risk-${r.from}`}
                      data-testid={`low-balance-line-${row.account_id}`}
                      className="text-[10px] tabular-nums text-danger"
                    >
                      {r.from === r.through
                        ? `Below zero on ${r.from} (${signedMoney(r.lowest_balance, pendingCurrencySymbol)})`
                        : `Below zero ${r.from} to ${r.through}, lowest ${signedMoney(r.lowest_balance, pendingCurrencySymbol)} on ${r.lowest_on}`}
                    </p>
                  ))}
                  {(row.cc_payments ?? []).map((p, i) => (
                    <p
                      key={`${p.date}-${i}`}
                      className="text-[10px] tabular-nums text-text-muted"
                    >
                      Payment {pendingCurrencySymbol}
                      {formatAmount(p.amount)} on {p.date}
                      {i === 0 && (
                        <>
                          {" "}
                          <Link
                            href={`/accounts?edit=${row.account_id}`}
                            className={btnLink}
                          >
                            Change
                          </Link>
                        </>
                      )}
                    </p>
                  ))}
                  {(row.loan_payments ?? []).map((p, i) => (
                    <p
                      key={`loan-${p.date}-${i}`}
                      className="text-[10px] tabular-nums text-text-muted"
                    >
                      Payment {pendingCurrencySymbol}
                      {formatAmount(p.amount)} on {p.date}
                      {i === 0 && (
                        <>
                          {" "}
                          <Link
                            href={`/accounts?edit=${row.account_id}`}
                            className={btnLink}
                          >
                            Change
                          </Link>
                        </>
                      )}
                    </p>
                  ))}
                  {/* TBD-198. Upcoming recurring occurrences, in the SAME
                      visual slot the CC / loan payment lines occupy. Without
                      this the forecast could sit hundreds below the balance
                      with nothing on screen naming the difference: the CC and
                      loan halves of the sum each got a line, this half got
                      none. Signed, because a recurring template can be income;
                      quiet by default, i.e. absent entirely when empty. */}
                  {(row.recurring_lines ?? []).map((p, i) => (
                    <p
                      key={`recurring-${p.date}-${i}`}
                      data-testid={`recurring-line-${row.account_id}`}
                      className="text-[10px] tabular-nums text-text-muted"
                    >
                      Recurring {signedMoney(p.amount, pendingCurrencySymbol)}{" "}
                      on {p.date}
                    </p>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

// `{sign}{symbol}{magnitude}` — the convention the pending sub-line already
// uses ("Includes -€600.00 pending"). The naive `${symbol}${formatAmount(v)}`
// renders "€-100.00" four lines away from it (TBD-198 review, N8).
function signedMoney(value: number | string, symbol: string): string {
  const n = Number(value);
  return `${n < 0 ? "-" : "+"}${symbol}${formatAmount(Math.abs(n))}`;
}

// Best-effort symbol mapping. Falls back to the ISO code so unknown
// currencies still round-trip readable copy.
function currencySymbol(code: string): string {
  switch (code) {
    case "EUR":
      return "€";
    case "USD":
      return "$";
    case "GBP":
      return "£";
    default:
      return `${code} `;
  }
}
