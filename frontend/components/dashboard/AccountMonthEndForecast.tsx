"use client";

import { card, cardHeader, cardTitle } from "@/lib/styles";
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
}

export interface AccountMonthEndForecastResponse {
  period_start: string;
  period_end: string;
  totals: AccountMonthEndForecastTotal[];
  accounts: AccountMonthEndForecastRow[];
}

export interface AccountMonthEndForecastProps {
  forecast: AccountMonthEndForecastResponse | null;
  isCurrentPeriod: boolean;
  onJumpToCurrent?: () => void;
  hasAnyAccounts: boolean;
}

export default function AccountMonthEndForecast({
  forecast,
  isCurrentPeriod,
  onJumpToCurrent,
  hasAnyAccounts,
}: AccountMonthEndForecastProps) {
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

  // No accounts yet: defer to the page-level empty state. Render nothing
  // so the dashboard's existing "No accounts yet" tile owns this surface.
  if (!hasAnyAccounts) return null;

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
      <header className={`mb-3 ${cardHeader}`}>
        <h2 className={cardTitle}>Forecast</h2>
        <p className="mt-1 text-xs text-text-muted">
          Current balance plus pending items in this period.
        </p>
      </header>

      {totals.length > 0 && (
        <div className="mb-4 space-y-1">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted">
            Expected month-end balance
          </p>
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
          <p className="text-xs text-text-muted">Includes pending items in this period.</p>
        </div>
      )}

      <div className="overflow-hidden rounded-md border border-border-subtle">
        <div className="grid grid-cols-[1fr_auto_auto] items-center gap-x-4 border-b border-border-subtle bg-surface-overlay px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
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
            return (
              <div
                key={row.account_id}
                className="grid grid-cols-[1fr_auto_auto] items-center gap-x-4 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="flex items-center gap-2 text-sm text-text-primary">
                    <span className="truncate">{row.account_name}</span>
                    {row.is_default && (
                      <span className="rounded border border-border px-1.5 py-0.5 text-[9px] font-semibold text-text-secondary">
                        DEFAULT
                      </span>
                    )}
                  </p>
                </div>
                <p className="text-sm tabular-nums text-text-secondary">
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
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
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
