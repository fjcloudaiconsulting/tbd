"use client";

/**
 * CreditUtilizationWidget — "Credit card utilization" dashboard tile. Reads
 * activeAccounts (balances + limits) AND accountMonthEndForecast (Slice-3
 * cc_payments) from DashboardDataProvider, joins by account_id, renders one
 * banded bar per credit card sorted by utilization desc. A quiet "Next
 * payment" chip shows when the forecast projects one. Credit cards with no
 * limit set (null/0) but a nonzero balance render as a plain "No limit set"
 * row instead of a bar, since utilization can't be computed without a limit.
 */
import { useMemo } from "react";
import Link from "next/link";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";
import { creditUtilization } from "@/lib/credit";
import { formatAmount } from "@/lib/format";
import { badgeNeutral, card, cardHeader, cardTitle } from "@/lib/styles";

export default function CreditUtilizationWidget() {
  const { activeAccounts, accountMonthEndForecast } = useDashboard();

  const creditCards = useMemo(
    () => activeAccounts.filter((a) => a.account_type_slug === "credit_card"),
    [activeAccounts],
  );

  const withLimit = useMemo(
    () =>
      creditCards
        .filter((a) => Number(a.credit_limit) > 0)
        .slice()
        .sort(
          (a, b) =>
            creditUtilization(Number(b.balance), Number(b.credit_limit)).utilizationPct -
            creditUtilization(Number(a.balance), Number(a.credit_limit)).utilizationPct,
        ),
    [creditCards],
  );

  const noLimit = useMemo(
    () =>
      creditCards.filter(
        (a) => !(Number(a.credit_limit) > 0) && Number(a.balance) !== 0,
      ),
    [creditCards],
  );

  const nextPaymentByAccount = useMemo(() => {
    const map: Record<number, { amount: string; date: string }> = {};
    for (const row of accountMonthEndForecast?.accounts ?? []) {
      const first = row.cc_payments?.[0];
      if (first) map[row.account_id] = first;
    }
    return map;
  }, [accountMonthEndForecast]);

  return (
    <div className={`${card} flex flex-col overflow-hidden`}>
      <div className={`flex items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Credit card utilization</h2>
        <Link
          href="/accounts"
          className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
        >
          Accounts
        </Link>
      </div>
      {creditCards.length === 0 ? (
        <div className="px-5 py-6 text-center text-sm text-text-muted">
          No credit cards yet.{" "}
          <Link
            href="/accounts"
            className="text-text-primary underline underline-offset-2 hover:text-text-secondary"
          >
            Add one
          </Link>
        </div>
      ) : (
        <div className="flex flex-col gap-4 p-4">
          {withLimit.map((a) => {
            const next = nextPaymentByAccount[a.id];
            return (
              <div key={a.id} className="flex flex-col gap-1.5">
                <CreditUtilizationBar
                  name={a.name}
                  balance={Number(a.balance)}
                  creditLimit={Number(a.credit_limit)}
                  currency={a.currency}
                />
                {next && (
                  <span className={badgeNeutral}>
                    Next payment {formatAmount(next.amount)} {a.currency} on {next.date}
                  </span>
                )}
              </div>
            );
          })}
          {noLimit.map((a) => (
            <div key={a.id} className="flex items-center justify-between text-xs text-text-muted">
              <span className="truncate">{a.name}</span>
              <span>No limit set</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
