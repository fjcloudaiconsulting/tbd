"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import type { Budget, Category } from "@/lib/types";

interface BillingPeriod {
  id: number;
  start_date: string;
  end_date: string | null;
}

export default function BudgetsPage() {
  const { user, loading } = useAuth();
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [periodIdx, setPeriodIdx] = useState(0);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);

  const [formCategoryId, setFormCategoryId] = useState<number | "">("");
  const [formAmount, setFormAmount] = useState("");

  // Edit
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editAmount, setEditAmount] = useState("");

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : null;
  const periodStart = selectedPeriod?.start_date ?? "";
  const isCurrentPeriod = selectedPeriod?.end_date === null;

  const loadRefs = useCallback(async () => {
    const [c, p] = await Promise.all([
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
    ]);
    setCategories(c ?? []);
    const pl = p ?? [];
    setPeriods(pl);
    // Default to current period (open = no end_date), not index 0
    const currentIdx = pl.findIndex((bp) => bp.end_date === null);
    if (currentIdx >= 0) setPeriodIdx(currentIdx);
  }, []);

  const loadBudgets = useCallback(async () => {
    const url = periodStart ? `/api/v1/budgets?period_start=${periodStart}` : "/api/v1/budgets";
    const b = await apiFetch<Budget[]>(url);
    setBudgets(b ?? []);
    setFetching(false);
  }, [periodStart]);

  useEffect(() => {
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      setFetching(true);
      loadBudgets().catch(() => setFetching(false));
    }
  }, [loading, user, loadBudgets]);

  // Master categories that don't have a budget yet
  const masterCategories = categories.filter((c) => c.parent_id === null && c.type === "expense");
  const budgetedCatIds = new Set(budgets.map((b) => b.category_id));
  const availableCategories = masterCategories.filter((c) => !budgetedCatIds.has(c.id));

  async function handleAdd(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const url = periodStart ? `/api/v1/budgets?period_start=${periodStart}` : "/api/v1/budgets";
      await apiFetch(url, {
        method: "POST",
        body: JSON.stringify({ category_id: formCategoryId, amount: formAmount }),
      });
      setFormCategoryId(""); setFormAmount(""); setShowForm(false);
      await loadBudgets();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleUpdate(id: number) {
    setError("");
    try {
      await apiFetch(`/api/v1/budgets/${id}`, {
        method: "PUT",
        body: JSON.stringify({ amount: editAmount }),
      });
      setEditingId(null);
      await loadBudgets();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(id: number) {
    if (!confirm("Remove this budget?")) return;
    try {
      await apiFetch(`/api/v1/budgets/${id}`, { method: "DELETE" });
      await loadBudgets();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  const totalBudget = budgets.reduce((s, b) => s + Number(b.amount), 0);
  const totalSpent = budgets.reduce((s, b) => s + Number(b.spent), 0);

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Budgets</h1>
        {availableCategories.length > 0 && (
          <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
            {showForm ? "Cancel" : "+ Add Budget"}
          </button>
        )}
      </div>

      {/* Period navigation */}
      {periods.length > 0 && (
        <div className="mb-5 flex items-center gap-3">
          <button onClick={() => setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1))} disabled={periodIdx >= periods.length - 1} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Previous period">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
          </button>
          <span className="text-sm text-text-secondary">
            {selectedPeriod?.start_date}{selectedPeriod?.end_date ? ` — ${selectedPeriod.end_date}` : ""}
            {isCurrentPeriod && <span className="ml-2 text-xs text-success font-medium">current</span>}
          </span>
          <button onClick={() => setPeriodIdx(Math.max(periodIdx - 1, 0))} disabled={periodIdx <= 0} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Next period">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
          </button>
          {!isCurrentPeriod && (
            <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) setPeriodIdx(idx); }} className="ml-1 rounded-md px-2 py-1 text-[11px] font-medium text-text-muted hover:bg-surface-raised">Today</button>
          )}
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {showForm && (
        <div className={`mb-6 ${card} p-6`}>
          <form onSubmit={handleAdd} className="flex flex-wrap gap-4 items-end">
            <div className="flex-1 min-w-[200px]">
              <label htmlFor="b-cat" className={label}>Category</label>
              <select id="b-cat" required value={formCategoryId} onChange={(e) => setFormCategoryId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select category</option>
                {availableCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
            <div className="w-40">
              <label htmlFor="b-amount" className={label}>Monthly limit</label>
              <input id="b-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <button type="submit" className={btnPrimary}>Add</button>
          </form>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Summary */}
          {budgets.length > 0 && (
            <div className="flex gap-4">
              <div className={`flex-1 ${card} p-5`}>
                <p className={cardTitle}>Total Budget</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums text-text-primary">{formatAmount(totalBudget)}</p>
              </div>
              <div className={`flex-1 ${card} p-5`}>
                <p className={cardTitle}>Total Spent</p>
                <p className={`mt-1 text-2xl font-semibold tabular-nums ${totalSpent > totalBudget ? "text-danger" : "text-text-primary"}`}>{formatAmount(totalSpent)}</p>
              </div>
              <div className={`flex-1 ${card} p-5`}>
                <p className={cardTitle}>Remaining</p>
                <p className={`mt-1 text-2xl font-semibold tabular-nums ${totalBudget - totalSpent < 0 ? "text-danger" : "text-success"}`}>{formatAmount(totalBudget - totalSpent)}</p>
              </div>
            </div>
          )}

          {/* Budget chart */}
          {budgets.length > 0 && (
            <div className={`${card} p-5`}>
              <div className="flex items-center justify-between mb-4">
                <h2 className={cardTitle}>Budget Overview</h2>
                <span className="text-xs text-text-muted">
                  {selectedPeriod && <>{selectedPeriod.start_date}{selectedPeriod.end_date ? ` — ${selectedPeriod.end_date}` : " (open)"}</>}
                </span>
              </div>
              <div style={{ height: Math.max(budgets.length * 48, 120) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={budgets.map((b) => ({
                    name: b.category_name,
                    spent: Number(b.spent),
                    remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
                    over: Math.max(Number(b.spent) - Number(b.amount), 0),
                    budget: Number(b.amount),
                    pct: b.percent_used,
                  }))} layout="vertical" margin={{ left: 10, right: 10, top: 0, bottom: 0 }}>
                    <XAxis type="number" hide />
                    <YAxis type="category" dataKey="name" width={120} tick={{ fill: "var(--color-text-secondary)", fontSize: 12 }} />
                    <Tooltip
                      formatter={(v, name) => [formatAmount(Number(v)), name === "spent" ? "Spent" : name === "remaining" ? "Remaining" : "Over budget"]}
                      contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: "6px", fontSize: "12px" }}
                    />
                    <Bar dataKey="spent" stackId="a" radius={[4, 0, 0, 4]} animationDuration={800}>
                      {budgets.map((b, i) => (
                        <Cell key={i} fill={b.percent_used > 100 ? "#f87171" : b.percent_used > 80 ? "#f59e0b" : "#4ade80"} />
                      ))}
                    </Bar>
                    <Bar dataKey="remaining" stackId="a" fill="var(--color-surface-overlay)" radius={[0, 4, 4, 0]} animationDuration={800} />
                    <Bar dataKey="over" stackId="a" fill="#f87171" radius={[0, 4, 4, 0]} animationDuration={800} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* Budget details */}
          <div className={card}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Details</h2>
            </div>
            <div className="divide-y divide-border-subtle">
              {budgets.map((b) => {
                const overBudget = b.percent_used > 100;
                return (
                  <div key={b.id} className="px-6 py-3">
                    {editingId === b.id ? (
                      <div className="flex items-center gap-3">
                        <span className="text-sm font-medium text-text-primary flex-1">{b.category_name}</span>
                        <input type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)}
                          className={`w-32 ${input}`} autoFocus
                          onKeyDown={(e) => { if (e.key === "Enter") handleUpdate(b.id); if (e.key === "Escape") setEditingId(null); }} />
                        <button onClick={() => handleUpdate(b.id)} className="text-xs text-accent hover:text-accent-hover">Save</button>
                        <button onClick={() => setEditingId(null)} className="text-xs text-text-muted hover:text-text-secondary">Cancel</button>
                      </div>
                    ) : (
                      <div className="flex items-center justify-between">
                        <span className="text-sm text-text-primary">{b.category_name}</span>
                        <div className="flex items-center gap-4">
                          <span className={`text-sm tabular-nums ${overBudget ? "text-danger font-medium" : "text-text-secondary"}`}>
                            {formatAmount(b.spent)} / {formatAmount(b.amount)}
                          </span>
                          <span className={`text-xs tabular-nums ${overBudget ? "text-danger" : "text-text-muted"}`}>
                            {b.percent_used}%
                          </span>
                          <div className="flex gap-2">
                            <button onClick={() => { setEditingId(b.id); setEditAmount(String(b.amount)); }} className="text-xs text-text-muted hover:text-accent">Edit</button>
                            <button onClick={() => handleDelete(b.id)} className="text-xs text-text-muted hover:text-danger">Remove</button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              {budgets.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  No budgets set. Click &quot;+ Add Budget&quot; to allocate spending limits for your categories.
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
