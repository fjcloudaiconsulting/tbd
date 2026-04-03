"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, formatLocalDate, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import CategorySelect from "@/components/ui/CategorySelect";
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
  const [filterSearch, setFilterSearch] = useState("");
  const [sortField, setSortField] = useState<"date" | "description" | "account_name" | "category_name" | "status" | "amount">("date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Billing periods for filter
  const [periods, setPeriods] = useState<{ id: number; start_date: string; end_date: string | null }[]>([]);
  const [filterPeriod, setFilterPeriod] = useState<string>("");

  // Form
  const [formMode, setFormMode] = useState<"transaction" | "transfer">("transaction");
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formToAccountId, setFormToAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formStatus, setFormStatus] = useState<"settled" | "pending">("settled");
  const [formDate, setFormDate] = useState(todayISO());
  const [formRecurring, setFormRecurring] = useState(false);
  const [formFrequency, setFormFrequency] = useState("monthly");
  const [formAutoSettle, setFormAutoSettle] = useState(false);

  const loadRefs = useCallback(async () => {
    const [accts, cats, pers] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<{ id: number; start_date: string; end_date: string | null }[]>("/api/v1/settings/billing-periods"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
    setPeriods(pers ?? []);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    let url = `/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}`;
    if (filterAccount) url += `&account_id=${filterAccount}`;
    if (filterCategory) url += `&category_id=${filterCategory}`;
    if (filterType) url += `&type=${filterType}`;
    if (filterStatus) url += `&status=${filterStatus}`;

    // Period filter overrides date_from/date_to
    if (filterPeriod) {
      const per = periods.find((p) => String(p.id) === filterPeriod);
      if (per) {
        url += `&date_from=${per.start_date}`;
        if (per.end_date) url += `&date_to=${per.end_date}`;
      }
    } else {
      if (filterDateFrom) url += `&date_from=${filterDateFrom}`;
      if (filterDateTo) url += `&date_to=${filterDateTo}`;
    }

    if (filterSearch) url += `&search=${encodeURIComponent(filterSearch)}`;
    const data = (await apiFetch<Transaction[]>(url)) ?? [];
    setHasMore(data.length > PAGE_SIZE);
    setTransactions(data.slice(0, PAGE_SIZE));
    setFetching(false);
  }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod, periods]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadTransactions(page).catch(() => setFetching(false));
    }
  }, [loading, user, loadTransactions, page]);

  useEffect(() => { setPage(0); }, [filterAccount, filterCategory, filterType, filterStatus, filterDateFrom, filterDateTo, filterSearch, filterPeriod]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      if (formMode === "transfer") {
        await apiFetch("/api/v1/transactions/transfer", {
          method: "POST",
          body: JSON.stringify({
            from_account_id: formAccountId,
            to_account_id: formToAccountId,
            description: formDescription,
            amount: formAmount,
            status: formStatus,
            date: formDate,
          }),
        });
      } else {
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
        // Create recurring template if repeat is enabled
        if (formRecurring && formMode === "transaction") {
          await apiFetch("/api/v1/recurring", {
            method: "POST",
            body: JSON.stringify({
              account_id: formAccountId,
              category_id: formCategoryId,
              description: formDescription,
              amount: formAmount,
              type: formType,
              frequency: formFrequency,
              next_due_date: formDate,
              auto_settle: formAutoSettle,
            }),
          });
        }
      }
      setFormDescription("");
      setFormAmount("");
      setFormType("expense");
      setFormStatus("settled");
      setFormToAccountId("");
      setFormRecurring(false);
      setFormAutoSettle(false);
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

  // Sort helper
  function toggleSort(field: typeof sortField) {
    if (sortField === field) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(field); setSortDir(field === "date" ? "desc" : "asc"); }
  }
  const sortedTransactions = [...transactions].sort((a, b) => {
    let cmp = 0;
    if (sortField === "date") cmp = a.date.localeCompare(b.date);
    else if (sortField === "description") cmp = a.description.localeCompare(b.description);
    else if (sortField === "account_name") cmp = a.account_name.localeCompare(b.account_name);
    else if (sortField === "category_name") cmp = a.category_name.localeCompare(b.category_name);
    else if (sortField === "status") cmp = a.status.localeCompare(b.status);
    else if (sortField === "amount") cmp = Number(a.amount) - Number(b.amount);
    return sortDir === "asc" ? cmp : -cmp;
  });
  const defaultAccount = activeAccounts.find((a) => a.is_default);

  // Pre-select default account when opening form
  useEffect(() => {
    if (showForm && formAccountId === "" && defaultAccount) {
      setFormAccountId(defaultAccount.id);
      if (defaultAccount.account_type_slug === "credit_card") setFormStatus("pending");
    }
  }, [showForm, formAccountId, defaultAccount]);

  function handleAccountChange(id: number | "") {
    setFormAccountId(id);
    if (formToAccountId === id) setFormToAccountId("");
    const acct = accounts.find((a) => a.id === id);
    setFormStatus(acct?.account_type_slug === "credit_card" ? "pending" : "settled");
  }

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
          <div className="mb-4 flex items-center gap-4">
            <h2 className={cardTitle}>{formMode === "transfer" ? "New Transfer" : "New Transaction"}</h2>
            <div className="flex rounded-md border border-border text-xs">
              <button type="button" onClick={() => setFormMode("transaction")} className={`px-3 py-1 rounded-l-md ${formMode === "transaction" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transaction</button>
              <button type="button" onClick={() => setFormMode("transfer")} className={`px-3 py-1 rounded-r-md ${formMode === "transfer" ? "bg-accent text-accent-text" : "text-text-muted hover:bg-surface-raised"}`}>Transfer</button>
            </div>
          </div>
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <label htmlFor="tx-account" className={label}>{formMode === "transfer" ? "From Account" : "Account"}</label>
              <select id="tx-account" required value={formAccountId} onChange={(e) => handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select account</option>
                {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            {formMode === "transfer" ? (
              <div>
                <label htmlFor="tx-to-account" className={label}>To Account</label>
                <select id="tx-to-account" required value={formToAccountId} onChange={(e) => setFormToAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                  <option value="">Select account</option>
                  {activeAccounts.filter((a) => a.id !== formAccountId).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </div>
            ) : (
              <div>
                <label htmlFor="tx-type" className={label}>Type</label>
                <select id="tx-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                  <option value="expense">Expense</option>
                  <option value="income">Income</option>
                </select>
              </div>
            )}
            {formMode === "transaction" && (
              <div>
                <label htmlFor="tx-category" className={label}>Category</label>
                <CategorySelect id="tx-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} />
              </div>
            )}
            <div>
              <label htmlFor="tx-desc" className={label}>Description</label>
              <input id="tx-desc" type="text" required placeholder="What was it for?" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="tx-amount" className={label}>Amount</label>
              <input id="tx-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
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
            {formMode === "transaction" && (
              <div className="flex items-end gap-4">
                <label className="flex items-center gap-2 text-sm text-text-secondary">
                  <input type="checkbox" checked={formRecurring} onChange={(e) => setFormRecurring(e.target.checked)} className="rounded border-border" />
                  Repeats
                </label>
                {formRecurring && (
                  <>
                    <div>
                      <label htmlFor="tx-freq" className="sr-only">Frequency</label>
                      <select id="tx-freq" value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} className={input}>
                        <option value="weekly">Weekly</option>
                        <option value="biweekly">Every 2 weeks</option>
                        <option value="monthly">Monthly</option>
                        <option value="quarterly">Quarterly</option>
                        <option value="yearly">Yearly</option>
                      </select>
                    </div>
                    <label className="flex items-center gap-2 text-xs text-text-muted">
                      <input type="checkbox" checked={formAutoSettle} onChange={(e) => setFormAutoSettle(e.target.checked)} className="rounded border-border" />
                      Auto-settle
                    </label>
                  </>
                )}
              </div>
            )}
            <div className="flex items-end">
              <button type="submit" className={btnPrimary}>{formMode === "transfer" ? "Transfer" : "Add Transaction"}</button>
            </div>
          </form>
        </div>
      )}

      {/* Search + Preset filters */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-[200px]">
          <label htmlFor="f-search" className="sr-only">Search transactions</label>
          <input id="f-search" type="text" placeholder="Search descriptions..." value={filterSearch} onChange={(e) => setFilterSearch(e.target.value)} className={input} />
        </div>
        <div className="flex gap-1">
          {[
            { label: "Today", fn: () => { const d = todayISO(); setFilterDateFrom(d); setFilterDateTo(d); } },
            { label: "This Week", fn: () => { const now = new Date(); const day = now.getDay(); const diff = day === 0 ? 6 : day - 1; const mon = new Date(now); mon.setDate(now.getDate() - diff); setFilterDateFrom(formatLocalDate(mon)); setFilterDateTo(todayISO()); } },
            { label: "This Month", fn: () => { const now = new Date(); setFilterDateFrom(formatLocalDate(new Date(now.getFullYear(), now.getMonth(), 1))); setFilterDateTo(todayISO()); } },
            { label: "All", fn: () => { setFilterDateFrom(""); setFilterDateTo(""); } },
          ].map((p) => (
            <button key={p.label} type="button" onClick={p.fn} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised">
              {p.label}
            </button>
          ))}
        </div>
      </div>
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
        {periods.length > 0 && (
          <div>
            <label htmlFor="f-period" className="sr-only">Billing period</label>
            <select id="f-period" value={filterPeriod} onChange={(e) => { setFilterPeriod(e.target.value); if (e.target.value) { setFilterDateFrom(""); setFilterDateTo(""); } }} className={`w-52 ${input}`}>
              <option value="">All periods</option>
              {periods.map((p) => (
                <option key={p.id} value={String(p.id)}>
                  {p.start_date}{p.end_date ? ` — ${p.end_date}` : " (current)"}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {fetching ? (
        <Spinner />
      ) : (
        <>
          <div className={card}>
            <div className="border-b border-border px-6 py-3">
              <div className="grid grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wider text-text-muted">
                {([
                  { field: "date" as const, label: "Date", span: "col-span-2", align: "" },
                  { field: "description" as const, label: "Description", span: "col-span-3", align: "" },
                  { field: "account_name" as const, label: "Account", span: "col-span-2", align: "" },
                  { field: "category_name" as const, label: "Category", span: "col-span-2", align: "" },
                  { field: "status" as const, label: "Status", span: "col-span-1", align: "text-center" },
                  { field: "amount" as const, label: "Amount", span: "col-span-1", align: "text-right" },
                ]).map((col) => (
                  <button key={col.field} onClick={() => toggleSort(col.field)} className={`${col.span} ${col.align} hover:text-text-primary transition-colors`}>
                    {col.label}{sortField === col.field ? (sortDir === "asc" ? " ↑" : " ↓") : ""}
                  </button>
                ))}
                <span className="col-span-1" />
              </div>
            </div>
            <div className="divide-y divide-border-subtle">
              {(() => {
                // Precompute tx map for O(1) lookups
                const txMap = new Map(transactions.map((t) => [t.id, t]));
                // Deduplicate transfers: keep the lower id (expense side)
                const hiddenIds = new Set<number>();
                for (const t of transactions) {
                  if (t.linked_transaction_id && t.id > t.linked_transaction_id) {
                    hiddenIds.add(t.id);
                  }
                }
                return sortedTransactions.filter((t) => !hiddenIds.has(t.id)).map((tx) => {
                const isTransfer = tx.linked_transaction_id !== null;
                const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                return editingId === tx.id ? (
                  <div key={tx.id} className="grid grid-cols-12 items-center gap-2 px-6 py-2 bg-surface-raised">
                    <span className="col-span-2"><input aria-label="Date" type="date" value={editDate} onChange={(e) => setEditDate(e.target.value)} className={`text-sm ${input}`} /></span>
                    <span className="col-span-2"><input aria-label="Description" type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)} className={`text-sm ${input}`} /></span>
                    <span className="col-span-2">
                      <select aria-label="Account" value={editAccountId} onChange={(e) => setEditAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={`text-sm ${input}`}>
                        {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}{!a.is_active ? " (inactive)" : ""}</option>)}
                      </select>
                    </span>
                    <span className="col-span-2">
                      <CategorySelect aria-label="Category" id={`edit-cat-${tx.id}`} categories={categories} value={editCategoryId} onChange={setEditCategoryId} filterType={editType} className={`text-sm ${input}`} />
                    </span>
                    <span className="col-span-1">
                      <select aria-label="Status" value={editStatus} onChange={(e) => setEditStatus(e.target.value as "settled" | "pending")} className={`text-[11px] ${input}`}>
                        <option value="settled">Settled</option>
                        <option value="pending">Pending</option>
                      </select>
                    </span>
                    <span className="col-span-1 flex gap-1">
                      <select aria-label="Type" value={editType} onChange={(e) => { setEditType(e.target.value as "income" | "expense"); setEditCategoryId(""); }} className={`text-[11px] w-14 ${input}`}>
                        <option value="expense">-</option>
                        <option value="income">+</option>
                      </select>
                      <input aria-label="Amount" type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)} className={`text-sm w-20 ${input}`} />
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
                    <span className="col-span-2 text-sm text-text-secondary">
                      {isTransfer && linkedTx
                        ? <>{tx.account_name} &rarr; {linkedTx.account_name}</>
                        : tx.account_name}
                    </span>
                    <span className="col-span-2 text-sm text-text-secondary">{tx.category_name}</span>
                    <span className="col-span-1 text-center">
                      {isTransfer ? (
                        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-surface-overlay text-text-muted"}`}>
                          {tx.status}
                        </span>
                      ) : (
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
                      )}
                    </span>
                    <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${isTransfer ? "text-accent" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                      {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                    </span>
                    <span className="col-span-1 flex justify-end gap-2">
                      {!isTransfer && <button onClick={() => startEdit(tx)} aria-label={`Edit: ${tx.description}`} className="text-xs text-text-muted hover:text-accent">Edit</button>}
                      <button onClick={() => handleDelete(tx.id)} aria-label={`Delete: ${tx.description}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                    </span>
                  </div>
                );
                });
              })()}
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
