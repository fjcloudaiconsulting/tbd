"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Account, Category, Transaction } from "@/lib/types";

export default function TransactionsPage() {
  const { user, loading } = useAuth();
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState("");

  // Filters
  const [filterAccount, setFilterAccount] = useState<number | "">("");
  const [filterCategory, setFilterCategory] = useState<number | "">("");

  // Form
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

  const loadTransactions = useCallback(async () => {
    let url = "/api/v1/transactions?limit=100";
    if (filterAccount) url += `&account_id=${filterAccount}`;
    if (filterCategory) url += `&category_id=${filterCategory}`;
    const data = await apiFetch<Transaction[]>(url);
    setTransactions(data ?? []);
  }, [filterAccount, filterCategory]);

  useEffect(() => {
    if (!loading && user) {
      loadRefs().catch(() => {});
    }
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      loadTransactions().catch(() => {});
    }
  }, [loading, user, loadTransactions]);

  async function handleAdd(e: FormEvent) {
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
          date: formDate,
        }),
      });
      setFormDescription("");
      setFormAmount("");
      setFormType("expense");
      setFormDate(new Date().toISOString().slice(0, 10));
      setShowForm(false);
      await loadTransactions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this transaction?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${id}`, { method: "DELETE" });
      await loadTransactions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
    }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);

  const inputClass =
    "w-full rounded-md border border-border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className="font-display text-2xl text-text-primary">Transactions</h1>
        {activeAccounts.length > 0 && categories.length > 0 && (
          <button
            onClick={() => setShowForm(!showForm)}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
          >
            {showForm ? "Cancel" : "+ New Transaction"}
          </button>
        )}
      </div>

      {error && (
        <div className="mb-6 rounded-md bg-danger-dim px-4 py-3 text-sm text-danger">
          {error}
        </div>
      )}

      {/* New transaction form */}
      {showForm && (
        <div className="mb-6 rounded-lg border border-border bg-surface p-6">
          <h2 className="mb-4 text-xs font-medium uppercase tracking-wider text-text-muted">
            New Transaction
          </h2>
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Account
              </label>
              <select
                required
                value={formAccountId}
                onChange={(e) =>
                  setFormAccountId(e.target.value === "" ? "" : Number(e.target.value))
                }
                className={inputClass}
              >
                <option value="">Select account</option>
                {activeAccounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Category
              </label>
              <select
                required
                value={formCategoryId}
                onChange={(e) =>
                  setFormCategoryId(e.target.value === "" ? "" : Number(e.target.value))
                }
                className={inputClass}
              >
                <option value="">Select category</option>
                {categories.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Type
              </label>
              <select
                value={formType}
                onChange={(e) => setFormType(e.target.value as "income" | "expense")}
                className={inputClass}
              >
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Description
              </label>
              <input
                type="text"
                required
                placeholder="What was it for?"
                value={formDescription}
                onChange={(e) => setFormDescription(e.target.value)}
                className={inputClass}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Amount
              </label>
              <input
                type="number"
                step="0.01"
                min="0.01"
                required
                placeholder="0.00"
                value={formAmount}
                onChange={(e) => setFormAmount(e.target.value)}
                className={inputClass}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
                Date
              </label>
              <input
                type="date"
                required
                value={formDate}
                onChange={(e) => setFormDate(e.target.value)}
                className={inputClass}
              />
            </div>
            <div className="flex items-end sm:col-span-2 lg:col-span-3">
              <button
                type="submit"
                className="rounded-md bg-accent px-5 py-2 text-sm font-medium text-accent-text hover:bg-accent-hover"
              >
                Add Transaction
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <select
          value={filterAccount}
          onChange={(e) =>
            setFilterAccount(e.target.value === "" ? "" : Number(e.target.value))
          }
          className={`w-48 ${inputClass}`}
        >
          <option value="">All accounts</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>{a.name}</option>
          ))}
        </select>
        <select
          value={filterCategory}
          onChange={(e) =>
            setFilterCategory(e.target.value === "" ? "" : Number(e.target.value))
          }
          className={`w-48 ${inputClass}`}
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
      </div>

      {/* Transaction list */}
      <div className="rounded-lg border border-border bg-surface">
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
            <div
              key={tx.id}
              className="grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised"
            >
              <span className="col-span-2 text-sm tabular-nums text-text-secondary">
                {tx.date}
              </span>
              <span className="col-span-3 text-sm text-text-primary">
                {tx.description}
              </span>
              <span className="col-span-2 text-sm text-text-secondary">
                {tx.account_name}
              </span>
              <span className="col-span-2 text-sm text-text-secondary">
                {tx.category_name}
              </span>
              <span
                className={`col-span-2 text-right text-sm font-medium tabular-nums ${
                  tx.type === "income" ? "text-success" : "text-danger"
                }`}
              >
                {tx.type === "income" ? "+" : "-"}
                {Number(tx.amount).toLocaleString("en", {
                  minimumFractionDigits: 2,
                })}
              </span>
              <span className="col-span-1 text-right">
                <button
                  onClick={() => handleDelete(tx.id)}
                  className="text-xs text-text-muted hover:text-danger"
                >
                  Delete
                </button>
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
    </AppShell>
  );
}
