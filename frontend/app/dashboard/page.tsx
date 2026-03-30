"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { card, cardHeader, cardTitle, pageTitle } from "@/lib/styles";
import type { Account } from "@/lib/types";

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [fetching, setFetching] = useState(true);

  useEffect(() => {
    if (!loading && user) {
      apiFetch<Account[]>("/api/v1/accounts")
        .then((data) => setAccounts(data ?? []))
        .catch(() => {})
        .finally(() => setFetching(false));
    }
  }, [loading, user]);

  const activeAccounts = accounts.filter((a) => a.is_active);

  const balanceByCurrency = activeAccounts.reduce<Record<string, number>>(
    (acc, a) => {
      const cur = a.currency || "EUR";
      acc[cur] = (acc[cur] || 0) + Number(a.balance);
      return acc;
    },
    {}
  );
  const currencies = Object.entries(balanceByCurrency);

  return (
    <AppShell>
      <h1 className={pageTitle}>Dashboard</h1>

      {fetching ? (
        <Spinner />
      ) : activeAccounts.length === 0 ? (
        <div className={`${card} p-10 text-center`}>
          <p className="text-text-secondary">No accounts yet.</p>
          <p className="mt-2 text-sm text-text-muted">
            Go to{" "}
            <Link href="/accounts" className="text-accent hover:text-accent-hover">Accounts</Link>{" "}
            to create your first account.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          <div className="flex gap-4">
            {currencies.map(([currency, total]) => (
              <div key={currency} className={`flex-1 ${card} p-6`}>
                <p className={cardTitle}>Total Balance</p>
                <p className="mt-2 font-display text-3xl text-accent">
                  {formatAmount(total)}
                  <span className="ml-2 text-lg text-text-muted">{currency}</span>
                </p>
              </div>
            ))}
          </div>

          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Accounts</h2>
            </div>
            <div className="divide-y divide-border-subtle">
              {activeAccounts.map((account) => (
                <div key={account.id} className="flex items-center justify-between px-6 py-4">
                  <div>
                    <p className="text-sm font-medium text-text-primary">{account.name}</p>
                    <p className="mt-0.5 text-xs text-text-muted">{account.account_type_name}</p>
                  </div>
                  <p className="text-sm tabular-nums text-text-primary">
                    {formatAmount(account.balance)}{" "}
                    <span className="text-text-muted">{account.currency}</span>
                  </p>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
