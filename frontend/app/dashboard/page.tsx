"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";
import CategorySelect from "@/components/ui/CategorySelect";
import type { Account, Category, Transaction } from "@/lib/types";

function formatLocalDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function currentMonthRange(): { from: string; to: string } {
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth();
  return {
    from: formatLocalDate(new Date(y, m, 1)),
    to: formatLocalDate(new Date(y, m + 1, 0)),
  };
}

const PAGE_SIZE = 10;

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");

  // Quick-add form
  const [showForm, setShowForm] = useState(false);
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formStatus, setFormStatus] = useState<"settled" | "pending">("settled");
  const [formDate, setFormDate] = useState(todayISO());

  const { from: monthFrom, to: monthTo } = currentMonthRange();

  const loadRefs = useCallback(async () => {
    const [accts, cats] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    const url = `/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}&date_from=${monthFrom}&date_to=${monthTo}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [monthFrom, monthTo]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleQuickAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/transactions", {
        method: "POST",
        body: JSON.stringify({
          account_id: formAccountId,
          category_id: formCategoryId,
          description: formDescription,
          amount: formAmount,
          type: formType,
          status: formStatus,
          date: formDate,
        }),
      });
      setFormDescription("");
      setFormAmount("");
      setFormType("expense");
      setFormStatus("settled");
      setFormDate(todayISO());
      setShowForm(false);
      await Promise.all([loadRefs(), loadTransactions(page)]);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);
  const canAdd = activeAccounts.length > 0 && categories.length > 0;

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
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
        {canAdd && (
          <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
            {showForm ? "Cancel" : "+ Quick Add"}
          </button>
        )}
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Quick-add form */}
          {showForm && (
            <div className={`${card} p-6`}>
              <h2 className={`mb-4 ${cardTitle}`}>New Transaction</h2>
              <form onSubmit={handleQuickAdd} className="grid grid-cols-2 gap-4 lg:grid-cols-4">
                <div>
                  <label htmlFor="da-account" className={label}>Account</label>
                  <select id="da-account" required value={formAccountId} onChange={(e) => setFormAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                    <option value="">Select account</option>
                    {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor="da-category" className={label}>Category</label>
                  <CategorySelect id="da-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} />
                </div>
                <div>
                  <label htmlFor="da-desc" className={label}>Description</label>
                  <input id="da-desc" type="text" required placeholder="What was it for?" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
                </div>
                <div>
                  <label htmlFor="da-amount" className={label}>Amount</label>
                  <input id="da-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
                </div>
                <div>
                  <label htmlFor="da-type" className={label}>Type</label>
                  <select id="da-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                    <option value="expense">Expense</option>
                    <option value="income">Income</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="da-status" className={label}>Status</label>
                  <select id="da-status" value={formStatus} onChange={(e) => setFormStatus(e.target.value as "settled" | "pending")} className={input}>
                    <option value="settled">Settled</option>
                    <option value="pending">Pending</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="da-date" className={label}>Date</label>
                  <input id="da-date" type="date" required value={formDate} onChange={(e) => setFormDate(e.target.value)} className={input} />
                </div>
                <div className="flex items-end">
                  <button type="submit" className={btnPrimary}>Add</button>
                </div>
              </form>
            </div>
          )}

          {/* Balance cards */}
          {currencies.length > 0 && (
            <div className="flex gap-4">
              {currencies.map(([currency, total]) => (
                <div key={currency} className={`flex-1 ${card} p-6`}>
                  <p className={cardTitle}>Balance</p>
                  <p className="mt-2 font-display text-3xl text-accent">
                    {formatAmount(total)}
                    <span className="ml-2 text-lg text-text-muted">{currency}</span>
                  </p>
                </div>
              ))}
            </div>
          )}

          {/* Recent transactions (current month) */}
          <div className={card}>
            <div className={`flex items-center justify-between ${cardHeader}`}>
              <h2 className={cardTitle}>Transactions — This Month</h2>
              <Link href="/transactions" className="text-xs text-accent hover:text-accent-hover">
                View All
              </Link>
            </div>
            <div className="divide-y divide-border-subtle">
              {transactions.map((tx) => (
                <div key={tx.id} className="flex items-center justify-between px-6 py-3">
                  <div className="flex items-center gap-4">
                    <span className="text-sm tabular-nums text-text-muted w-20">{tx.date}</span>
                    <div>
                      <p className="text-sm text-text-primary">{tx.description}</p>
                      <p className="text-xs text-text-muted">
                        {tx.account_name} · {tx.category_name}
                        {tx.status === "pending" && (
                          <span className="ml-1.5 rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">
                            pending
                          </span>
                        )}
                      </p>
                    </div>
                  </div>
                  <span className={`text-sm font-medium tabular-nums ${tx.type === "income" ? "text-success" : "text-danger"}`}>
                    {tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                  </span>
                </div>
              ))}
              {transactions.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  {!canAdd
                    ? "Create accounts and categories first."
                    : "No transactions this month."}
                </div>
              )}
            </div>

            {/* Pagination */}
            {(page > 0 || hasMore) && (
              <div className="flex items-center justify-between border-t border-border px-6 py-3">
                <button
                  onClick={() => setPage(Math.max(0, page - 1))}
                  disabled={page === 0}
                  className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40"
                >
                  Previous
                </button>
                <span className="text-xs text-text-muted">Page {page + 1}</span>
                <button
                  onClick={() => setPage(page + 1)}
                  disabled={!hasMore}
                  className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            )}
          </div>

          {/* Empty state for no accounts */}
          {activeAccounts.length === 0 && (
            <div className={`${card} p-10 text-center`}>
              <p className="text-text-secondary">No accounts yet.</p>
              <p className="mt-2 text-sm text-text-muted">
                Go to{" "}
                <Link href="/accounts" className="text-accent hover:text-accent-hover">Accounts</Link>{" "}
                to create your first account.
              </p>
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}
