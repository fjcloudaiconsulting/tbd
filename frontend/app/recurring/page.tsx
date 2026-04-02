"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import CategorySelect from "@/components/ui/CategorySelect";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, success as successCls, pageTitle } from "@/lib/styles";
import type { Account, Category, RecurringTransaction } from "@/lib/types";

const FREQ_LABELS: Record<string, string> = {
  weekly: "Weekly",
  biweekly: "Every 2 weeks",
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
};

export default function RecurringPage() {
  const { user, loading } = useAuth();
  const [items, setItems] = useState<RecurringTransaction[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");
  const [showForm, setShowForm] = useState(false);

  // Form
  const [formAccountId, setFormAccountId] = useState<number | "">("");
  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formDescription, setFormDescription] = useState("");
  const [formAmount, setFormAmount] = useState("");
  const [formType, setFormType] = useState<"income" | "expense">("expense");
  const [formFrequency, setFormFrequency] = useState("monthly");
  const [formNextDue, setFormNextDue] = useState(todayISO());
  const [formAutoSettle, setFormAutoSettle] = useState(false);

  const reload = useCallback(async () => {
    const [r, a, c] = await Promise.all([
      apiFetch<RecurringTransaction[]>("/api/v1/recurring"),
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
    ]);
    setItems(r ?? []);
    setAccounts(a ?? []);
    setCategories(c ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  const activeAccounts = accounts.filter((a) => a.is_active);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/recurring", {
        method: "POST",
        body: JSON.stringify({
          account_id: formAccountId,
          category_id: formCategoryId,
          description: formDescription,
          amount: formAmount,
          type: formType,
          frequency: formFrequency,
          next_due_date: formNextDue,
          auto_settle: formAutoSettle,
        }),
      });
      setFormDescription(""); setFormAmount(""); setFormType("expense");
      setFormFrequency("monthly"); setFormNextDue(todayISO()); setFormAutoSettle(false);
      setShowForm(false);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleToggleActive(item: RecurringTransaction) {
    try {
      await apiFetch(`/api/v1/recurring/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_active: !item.is_active }),
      });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this recurring transaction?")) return;
    try {
      await apiFetch(`/api/v1/recurring/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleGenerate() {
    setError(""); setSuccessMsg("");
    try {
      const res = await apiFetch<{ generated: number }>("/api/v1/recurring/generate", { method: "POST" });
      setSuccessMsg(`Generated ${res?.generated ?? 0} transaction(s)`);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Recurring Transactions</h1>
        <div className="flex gap-2">
          <button onClick={handleGenerate} className="rounded-md border border-border px-4 py-2 text-sm text-text-secondary hover:bg-surface-raised">
            Generate Due
          </button>
          {activeAccounts.length > 0 && categories.length > 0 && (
            <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
              {showForm ? "Cancel" : "+ New Recurring"}
            </button>
          )}
        </div>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}
      {successMsg && <div className={`mb-6 ${successCls}`}>{successMsg}</div>}

      {showForm && (
        <div className={`mb-6 ${card} p-6`}>
          <h2 className={`mb-4 ${cardTitle}`}>New Recurring Transaction</h2>
          <form onSubmit={handleAdd} className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <label htmlFor="r-account" className={label}>Account</label>
              <select id="r-account" required value={formAccountId} onChange={(e) => setFormAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select account</option>
                {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="r-type" className={label}>Type</label>
              <select id="r-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                <option value="expense">Expense</option>
                <option value="income">Income</option>
              </select>
            </div>
            <div>
              <label htmlFor="r-category" className={label}>Category</label>
              <CategorySelect id="r-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} />
            </div>
            <div>
              <label htmlFor="r-desc" className={label}>Description</label>
              <input id="r-desc" type="text" required placeholder="e.g., Netflix, Rent" value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="r-amount" className={label}>Amount</label>
              <input id="r-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <div>
              <label htmlFor="r-freq" className={label}>Frequency</label>
              <select id="r-freq" value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} className={input}>
                <option value="weekly">Weekly</option>
                <option value="biweekly">Every 2 weeks</option>
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
                <option value="yearly">Yearly</option>
              </select>
            </div>
            <div>
              <label htmlFor="r-next" className={label}>First Due Date</label>
              <input id="r-next" type="date" required value={formNextDue} onChange={(e) => setFormNextDue(e.target.value)} className={input} />
            </div>
            <div className="flex items-end gap-4">
              <label className="flex items-center gap-2 text-sm text-text-secondary">
                <input type="checkbox" checked={formAutoSettle} onChange={(e) => setFormAutoSettle(e.target.checked)} className="rounded border-border" />
                Auto-settle
              </label>
              <button type="submit" className={btnPrimary}>Add</button>
            </div>
          </form>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className={card}>
          <div className="border-b border-border px-6 py-3">
            <div className="grid grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wider text-text-muted">
              <span className="col-span-3">Description</span>
              <span className="col-span-2">Account</span>
              <span className="col-span-2">Category</span>
              <span className="col-span-1">Frequency</span>
              <span className="col-span-1">Next Due</span>
              <span className="col-span-1 text-right">Amount</span>
              <span className="col-span-2" />
            </div>
          </div>
          <div className="divide-y divide-border-subtle">
            {items.map((r) => (
              <div key={r.id} className={`grid grid-cols-12 items-center gap-4 px-6 py-3 transition-colors hover:bg-surface-raised ${!r.is_active ? "opacity-40" : ""}`}>
                <span className="col-span-3 text-sm text-text-primary">
                  {r.description}
                  {r.auto_settle && <span className="ml-1.5 rounded bg-success-dim px-1.5 py-0.5 text-[10px] font-medium text-success">auto</span>}
                </span>
                <span className="col-span-2 text-sm text-text-secondary">{r.account_name}</span>
                <span className="col-span-2 text-sm text-text-secondary">{r.category_name}</span>
                <span className="col-span-1 text-xs text-text-muted">{FREQ_LABELS[r.frequency] ?? r.frequency}</span>
                <span className="col-span-1 text-sm tabular-nums text-text-secondary">{r.next_due_date}</span>
                <span className={`col-span-1 text-right text-sm font-medium tabular-nums ${r.type === "income" ? "text-success" : "text-danger"}`}>
                  {r.type === "income" ? "+" : "-"}{formatAmount(r.amount)}
                </span>
                <span className="col-span-2 flex justify-end gap-2">
                  <button onClick={() => handleToggleActive(r)} className="text-xs text-text-muted hover:text-text-secondary">
                    {r.is_active ? "Pause" : "Resume"}
                  </button>
                  <button onClick={() => handleDelete(r.id)} className="text-xs text-text-muted hover:text-danger">Delete</button>
                </span>
              </div>
            ))}
            {items.length === 0 && (
              <div className="px-6 py-8 text-center text-sm text-text-muted">
                No recurring transactions. Add one to automate regular income or expenses.
              </div>
            )}
          </div>
        </div>
      )}
    </AppShell>
  );
}
