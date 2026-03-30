"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Account } from "@/lib/types";

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);

  useEffect(() => {
    if (!loading && user) {
      apiFetch<Account[]>("/api/v1/accounts").then(setAccounts).catch(() => {});
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
      <h1 className="mb-8 font-display text-2xl text-text-primary">Dashboard</h1>

      {activeAccounts.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface p-10 text-center">
          <p className="text-text-secondary">No accounts yet.</p>
          <p className="mt-2 text-sm text-text-muted">
            Go to{" "}
            <Link href="/accounts" className="text-accent hover:text-accent-hover">
              Accounts
            </Link>{" "}
            to create your first account.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Balance cards */}
          <div className="flex gap-4">
            {currencies.map(([currency, total]) => (
              <div
                key={currency}
                className="flex-1 rounded-lg border border-border bg-surface p-6"
              >
                <p className="text-xs font-medium uppercase tracking-wider text-text-muted">
                  Total Balance
                </p>
                <p className="mt-2 font-display text-3xl text-accent">
                  {total.toLocaleString("en", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                  <span className="ml-2 text-lg text-text-muted">{currency}</span>
                </p>
              </div>
            ))}
          </div>

          {/* Account list */}
          <div className="rounded-lg border border-border bg-surface">
            <div className="border-b border-border px-6 py-4">
              <h2 className="text-xs font-medium uppercase tracking-wider text-text-muted">
                Accounts
              </h2>
            </div>
            <div className="divide-y divide-border-subtle">
              {activeAccounts.map((account) => (
                <div
                  key={account.id}
                  className="flex items-center justify-between px-6 py-4"
                >
                  <div>
                    <p className="text-sm font-medium text-text-primary">{account.name}</p>
                    <p className="mt-0.5 text-xs text-text-muted">
                      {account.account_type_name}
                    </p>
                  </div>
                  <p className="text-sm tabular-nums text-text-primary">
                    {Number(account.balance).toLocaleString("en", {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}{" "}
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
