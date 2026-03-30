"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type { Account, Category, Transaction } from "@/lib/types";

const PAGE_SIZE = 20;

export default function TransactionsPage() {
  const { user, loading } = useAuth();
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState("");
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const [filterAccount, setFilterAccount] = useState<number | "">("");
  const [filterCategory, setFilterCategory] = useState<number | "">("");

  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formDate, setFormDate] = useState(new Date().toISOString().slice(0, 10));

  const loadRefs = useCallback(async () => {
    const [accts, cats] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    let url = `/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}`;
    if (filterAccount) url += `&account_id=${filterAccount}`;
    if (filterCategory) url += `&category_id=${filterCategory}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [filterAccount, filterCategory]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  // Reset page when filters change
  useEffect(() => { setPage(0); }, [filterAccount, filterCategory]);

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/transactions", {
        method: "POST",
        body: JSON.stringify({
          account_id: formAccountId, category_id: formCategoryId,
          description: formDescription, amount: formAmount, type: formType, date: formDate,
        }),
      });
      setFormDescription(""); setFormAmount(""); setFormType("expense");
      setFormDate(new Date().toISOString().slice(0, 10));
      setShowForm(false);
      await loadTransactions(page);
    } catch (err) { setError(err instanceof Error ? err.message : "Failed"); }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this transaction?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${id}`, { method: "DELETE" });
      await loadTransactions(page);
    } catch (err) { setError(err instanceof Error ? err.message : "Failed"); }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Transactions</h1>
        {activeAccounts.length > 0 && categories.length > 0 && (
          <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
            {showForm ? "Cancel" : "+ New Transaction"}
          </button>
        )}
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {showForm && (
        <div className={`mb-6 ${card} p-6`}>
          <h2 className={`mb-4 ${cardTitle}`}>New Transaction</h2>
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label htmlFor="tx-account" className={label}>Account</label>
              <select id="tx-account" required value={formAccountId} onChange={(e) => setFormAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select account</option>
                {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="tx-category" className={label}>Category</label>
              <select id="tx-category" required value={formCategoryId} onChange={(e) => setFormCategoryId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select category</option>
                {categories.filter((c) => c.type === "both" || c.type === formType).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="tx-type" className={label}>Type</label>
              <select id="tx-type" value={formType} onChange={(e) => setFormType(e.target.value as "income" | "expense")} className={input}>
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div>
              <label htmlFor="tx-desc" className={label}>Description</label>
              <input id="tx-desc" type="text" required placeholder="What was it for?" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-amount" className={label}>Amount</label>
              <input id="tx-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-date" className={label}>Date</label>
              <input id="tx-date" type="date" required value={formDate} onChange={(e) => setFormDate(e.target.value)} className={input} />
            </div>
            <div className="flex items-end sm:col-span-2 lg:col-span-3">
              <button type="submit" className={btnPrimary}>Add Transaction</button>
            </div>
          </form>
        </div>
      )}

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <div>
          <label htmlFor="filter-account" className="sr-only">Filter by account</label>
          <select id="filter-account" value={filterAccount} onChange={(e) => setFilterAccount(e.target.value === "" ? "" : Number(e.target.value))} className={`w-48 ${input}`}>
            <option value="">All accounts</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="filter-category" className="sr-only">Filter by category</label>
          <select id="filter-category" value={filterCategory} onChange={(e) => setFilterCategory(e.target.value === "" ? "" : Number(e.target.value))} className={`w-48 ${input}`}>
            <option value="">All categories</option>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
      </div>

      {fetching ? (
        <Spinner />
      ) : (
        <>
          <div className={card}>
            <div className="border-b border-border px-6 py-3">
              <div className="grid grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wider text-text-muted">
                <span className="col-span-2">Date</span>
                <span className="col-span-3">Description</span>
                <span className="col-span-2">Account</span>
                <span className="col-span-2">Category</span>
                <span className="col-span-2 text-right">Amount</span>
                <span className="col-span-1" />
              </div>
            </div>
            <div className="divide-y divide-border-subtle">
              {transactions.map((tx) => (
                <div key={tx.id} className="grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised">
                  <span className="col-span-2 text-sm tabular-nums text-text-secondary">{tx.date}</span>
                  <span className="col-span-3 text-sm text-text-primary">{tx.description}</span>
                  <span className="col-span-2 text-sm text-text-secondary">{tx.account_name}</span>
                  <span className="col-span-2 text-sm text-text-secondary">{tx.category_name}</span>
                  <span className={`col-span-2 text-right text-sm font-medium tabular-nums ${tx.type === "income" ? "text-success" : "text-danger"}`}>
                    {tx.type === "income" ? "+" : "-"}
                    {Number(tx.amount).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  <span className="col-span-1 text-right">
                    <button onClick={() => handleDelete(tx.id)} aria-label={`Delete transaction: ${tx.description}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                  </span>
                </div>
              ))}
              {transactions.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  {activeAccounts.length === 0
                    ? "Create an account first."
                    : categories.length === 0
                      ? "Create a category first."
                      : "No transactions yet. Click '+ New Transaction' to add one."}
                </div>
              )}
            </div>
          </div>

          {/* Pagination */}
          {(page > 0 || hasMore) && (
            <div className="mt-4 flex items-center justify-between">
              <button
                onClick={() => setPage(Math.max(0, page - 1))}
                disabled={page === 0}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40"
              >
                Previous
              </button>
              <span className="text-xs text-text-muted">Page {page + 1}</span>
              <button
                onClick={() => setPage(page + 1)}
                disabled={!hasMore}
                className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </AppShell>
  );
}
