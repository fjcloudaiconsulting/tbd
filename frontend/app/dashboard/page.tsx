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
      <h1 className="mb-6 text-xl font-semibold">Dashboard</h1>

      {activeAccounts.length === 0 ? (
        <div className="rounded-lg border border-gray-200 bg-white p-8 text-center text-gray-500">
          <p>No accounts yet.</p>
          <p className="mt-1 text-sm">
            Go to{" "}
            <Link href="/accounts" className="text-blue-600 hover:underline">
              Accounts
            </Link>{" "}
            to create your first account.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex gap-4">
            {currencies.map(([currency, total]) => (
              <div
                key={currency}
                className="flex-1 rounded-lg border border-gray-200 bg-white p-5"
              >
                <p className="text-sm text-gray-500">Total Balance</p>
                <p className="mt-1 text-2xl font-bold">
                  {total.toLocaleString("en", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}{" "}
                  <span className="text-base font-normal text-gray-400">
                    {currency}
                  </span>
                </p>
              </div>
            ))}
          </div>

          <div className="rounded-lg border border-gray-200 bg-white">
            <div className="border-b border-gray-100 px-5 py-3">
              <h2 className="text-sm font-medium text-gray-700">Accounts</h2>
            </div>
            <div className="divide-y divide-gray-100">
              {activeAccounts.map((account) => (
                <div
                  key={account.id}
                  className="flex items-center justify-between px-5 py-3"
                >
                  <div>
                    <p className="text-sm font-medium">{account.name}</p>
                    <p className="text-xs text-gray-400">
                      {account.account_type_name}
                    </p>
                  </div>
                  <p className="text-sm font-medium">
                    {Number(account.balance).toLocaleString("en", {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}{" "}
                    <span className="text-xs text-gray-400">
                      {account.currency}
                    </span>
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
