"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
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

  // Edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDesc, setEditDesc] = useState("");
  const [editAmount, setEditAmount] = useState("");
  const [editType, setEditType] = useState<"income" | "expense">("expense");
  const [editStatus, setEditStatus] = useState<"settled" | "pending">("settled");
  const [editDate, setEditDate] = useState("");
  const [editAccountId, setEditAccountId] = useState<number | "">("");
  const [editCategoryId, setEditCategoryId] = useState<number | "">("");

  // Filters
  const [filterAccount, setFilterAccount] = useState<number | "">("");
  const [filterCategory, setFilterCategory] = useState<number | "">("");
  const [filterType, setFilterType] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [filterDateFrom, setFilterDateFrom] = useState("");
  const [filterDateTo, setFilterDateTo] = useState("");

  // Form
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formStatus, setFormStatus] = useState<"settled" | "pending">("settled");
  const [formDate, setFormDate] = useState(todayISO());

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
    if (filterType) url += `&type=${filterType}`;
    if (filterStatus) url += `&status=${filterStatus}`;
    if (filterDateFrom) url += `&date_from=${filterDateFrom}`;
    if (filterDateTo) url += `&date_to=${filterDateTo}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  useEffect(() => { setPage(0); }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

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
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this transaction?")) return;
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${id}`, { method: "DELETE" });
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  function startEdit(tx: Transaction) {
    setEditingId(tx.id);
    setEditDesc(tx.description);
    setEditAmount(String(tx.amount));
    setEditType(tx.type);
    setEditStatus(tx.status);
    setEditDate(tx.date);
    setEditAccountId(tx.account_id);
    setEditCategoryId(tx.category_id);
  }

  async function handleSaveEdit() {
    if (editingId === null) return;
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${editingId}`, {
        method: "PUT",
        body: JSON.stringify({
          description: editDesc,
          amount: editAmount,
          type: editType,
          status: editStatus,
          date: editDate,
          account_id: editAccountId,
          category_id: editCategoryId,
        }),
      });
      setEditingId(null);
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  async function handleToggleStatus(tx: Transaction) {
    setError("");
    try {
      await apiFetch(`/api/v1/transactions/${tx.id}`, {
        method: "PUT",
        body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }),
      });
      await loadTransactions(page);
    } catch (err) {
      setError(extractErrorMessage(err));
    }
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
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
              <label htmlFor="tx-desc" className={label}>Description</label>
              <input id="tx-desc" type="text" required placeholder="What was it for?" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-amount" className={label}>Amount</label>
              <input id="tx-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-type" className={label}>Type</label>
              <select id="tx-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div>
              <label htmlFor="tx-status" className={label}>Status</label>
              <select id="tx-status" value={formStatus} onChange={(e) => setFormStatus(e.target.value as "settled" | "pending")} className={input}>
                <option value="settled">Settled</option>
                <option value="pending">Pending</option>
              </select>
            </div>
            <div>
              <label htmlFor="tx-date" className={label}>Date</label>
              <input id="tx-date" type="date" required value={formDate} onChange={(e) => setFormDate(e.target.value)} className={input} />
            </div>
            <div className="flex items-end">
              <button type="submit" className={btnPrimary}>Add Transaction</button>
            </div>
          </form>
        </div>
      )}

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <div>
          <label htmlFor="f-account" className="sr-only">Filter by account</label>
          <select id="f-account" value={filterAccount} onChange={(e) => setFilterAccount(e.target.value === "" ? "" : Number(e.target.value))} className={`w-40 ${input}`}>
            <option value="">All accounts</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="f-category" className="sr-only">Filter by category</label>
          <select id="f-category" value={filterCategory} onChange={(e) => setFilterCategory(e.target.value === "" ? "" : Number(e.target.value))} className={`w-40 ${input}`}>
            <option value="">All categories</option>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="f-type" className="sr-only">Filter by type</label>
          <select id="f-type" value={filterType} onChange={(e) => setFilterType(e.target.value)} className={`w-32 ${input}`}>
            <option value="">All types</option>
            <option value="income">Income</option>
            <option value="expense">Expense</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-status" className="sr-only">Filter by status</label>
          <select id="f-status" value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)} className={`w-32 ${input}`}>
            <option value="">All statuses</option>
            <option value="settled">Settled</option>
            <option value="pending">Pending</option>
          </select>
        </div>
        <div>
          <label htmlFor="f-from" className="sr-only">From date</label>
          <input id="f-from" type="date" value={filterDateFrom} onChange={(e) => setFilterDateFrom(e.target.value)} className={`w-36 ${input}`} placeholder="From" />
        </div>
        <div>
          <label htmlFor="f-to" className="sr-only">To date</label>
          <input id="f-to" type="date" value={filterDateTo} onChange={(e) => setFilterDateTo(e.target.value)} className={`w-36 ${input}`} placeholder="To" />
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
                <span className="col-span-1 text-center">Status</span>
                <span className="col-span-1 text-right">Amount</span>
                <span className="col-span-1" />
              </div>
            </div>
            <div className="divide-y divide-border-subtle">
              {transactions.map((tx) =>
                editingId === tx.id ? (
                  <div key={tx.id} className="grid grid-cols-12 items-center gap-2 px-6 py-2 bg-surface-raised">
                    <span className="col-span-2"><input type="date" value={editDate} onChange={(e) => setEditDate(e.target.value)} className={`text-sm ${input}`} /></span>
                    <span className="col-span-2"><input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className={`text-sm ${input}`} /></span>
                    <span className="col-span-2">
                      <select value={editAccountId} onChange={(e) => setEditAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={`text-sm ${input}`}>
                        {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                      </select>
                    </span>
                    <span className="col-span-2">
                      <select value={editCategoryId} onChange={(e) => setEditCategoryId(e.target.value === "" ? "" : Number(e.target.value))} className={`text-sm ${input}`}>
                        {categories.filter((c) => c.type === "both" || c.type === editType).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                    </span>
                    <span className="col-span-1">
                      <select value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-[11px] ${input}`}>
                        <option value="settled">Settled</option>
                        <option value="pending">Pending</option>
                      </select>
                    </span>
                    <span className="col-span-1 flex gap-1">
                      <select value={editType} onChange={(e) => setEditType(e.target.value as "income" | "expense")} className={`text-[11px] w-14 ${input}`}>
                        <option value="expense">-</option>
                        <option value="income">+</option>
                      </select>
                      <input type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} className={`text-sm w-20 ${input}`} />
                    </span>
                    <span className="col-span-2 flex justify-end gap-2">
                      <button onClick={handleSaveEdit} className="text-xs text-accent hover:text-accent-hover">Save</button>
                      <button onClick={() => setEditingId(null)} className="text-xs text-text-muted hover:text-text-secondary">Cancel</button>
                    </span>
                  </div>
                ) : (
                  <div key={tx.id} className={`grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised ${tx.status === "pending" ? "opacity-60" : ""}`}>
                    <span className="col-span-2 text-sm tabular-nums text-text-secondary">{tx.date}</span>
                    <span className="col-span-3 text-sm text-text-primary">{tx.description}</span>
                    <span className="col-span-2 text-sm text-text-secondary">{tx.account_name}</span>
                    <span className="col-span-2 text-sm text-text-secondary">{tx.category_name}</span>
                    <span className="col-span-1 text-center">
                      <button
                        onClick={() => handleToggleStatus(tx)}
                        aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                        className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                          tx.status === "settled"
                            ? "bg-success-dim text-success"
                            : "bg-surface-overlay text-text-muted"
                        }`}
                      >
                        {tx.status}
                      </button>
                    </span>
                    <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${tx.type === "income" ? "text-success" : "text-danger"}`}>
                      {tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                    </span>
                    <span className="col-span-1 flex justify-end gap-2">
                      <button onClick={() => startEdit(tx)} aria-label={`Edit: ${tx.description}`} className="text-xs text-text-muted hover:text-accent">Edit</button>
                      <button onClick={() => handleDelete(tx.id)} aria-label={`Delete: ${tx.description}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                    </span>
                  </div>
                )
              )}
              {transactions.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  {activeAccounts.length === 0
                    ? "Create an account first."
                    : categories.length === 0
                      ? "Create a category first."
                      : "No transactions match your filters."}
                </div>
              )}
            </div>
          </div>

          {(page > 0 || hasMore) && (
            <div className="mt-4 flex items-center justify-between">
              <button onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0} className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40">
                Previous
              </button>
              <span className="text-xs text-text-muted">Page {page + 1}</span>
              <button onClick={() => setPage(page + 1)} disabled={!hasMore} className="rounded-md border border-border px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-raised disabled:opacity-40">
                Next
              </button>
            </div>
          )}
        </>
      )}
    </AppShell>
  );
}
