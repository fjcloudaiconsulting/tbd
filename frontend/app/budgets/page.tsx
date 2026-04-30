"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import type { BillingPeriod, Budget, Category } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";

export default function BudgetsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
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

  // Transfer
  const [transferringId, setTransferringId] = useState<number | null>(null);
  const [transferCategoryId, setTransferCategoryId] = useState<number | "">("");
  const [transferAmount, setTransferAmount] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : null;
  const periodStart = selectedPeriod?.start_date ?? "";
  const isCurrentPeriod = selectedPeriod?.end_date === null;

  const loadRefs = useCallback(async () => {
    const [c, p] = await Promise.all([
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
    ]);
    setCategories(c ?? []);
    // Drop future stubs created by Forecasts /ensure-future. The Budget
    // page is current-period control + past read-only review only.
    const today = todayISO();
    const pl = (p ?? []).filter((bp) => bp.start_date <= today);
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

  // C+ plan: all mutations are current-period-only. If the user
  // navigates to a past period mid-edit, drop any open form/state so
  // they can't submit against a closed period.
  useEffect(() => {
    if (!isCurrentPeriod) {
      setShowForm(false);
      setEditingId(null);
      setTransferringId(null);
      setConfirmDeleteId(null);
    }
  }, [isCurrentPeriod]);

  // Master categories that don't have a budget yet
  const masterCategories = categories.filter((c) => c.parent_id === null && c.type === "expense");
  const budgetedCatIds = new Set(budgets.map((b) => b.category_id));
  const availableCategories = masterCategories.filter((c) => !budgetedCatIds.has(c.id));

  async function handleFromForecast() {
    setError("");
    try {
      const updated = await apiFetch<Budget[]>("/api/v1/budgets/from-forecast", { method: "POST" });
      setBudgets(updated ?? []);
    } catch (err) {
      // Most common case: no plan exists for the current period — the
      // backend's ValidationError message tells the user to create one
      // on the Forecasts page first.
      setError(extractErrorMessage(err));
    }
  }

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
    setConfirmDeleteId(null);
    try {
      await apiFetch(`/api/v1/budgets/${id}`, { method: "DELETE" });
      await loadBudgets();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleTransfer(fromId: number) {
    setError("");
    try {
      await apiFetch("/api/v1/budgets/transfer", {
        method: "POST",
        body: JSON.stringify({
          from_budget_id: fromId,
          to_category_id: transferCategoryId,
          amount: transferAmount,
        }),
      });
      setTransferringId(null);
      setTransferCategoryId("");
      setTransferAmount("");
      await loadBudgets();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  const totalBudget = budgets.reduce((s, b) => s + Number(b.amount), 0);
  const totalSpent = budgets.reduce((s, b) => s + Number(b.spent), 0);

  return (
    <AppShell>
      <div className="mb-6 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h1 className={`${pageTitle} mb-0`}>Budgets</h1>
        <div className="flex flex-wrap gap-2">
          {isCurrentPeriod && (
            <button
              onClick={handleFromForecast}
              className="min-h-[44px] sm:min-h-0 rounded-md border border-border-subtle bg-surface-raised px-4 py-2 text-sm font-medium text-text-primary hover:bg-surface-overlay"
            >
              From Forecast
            </button>
          )}
          {isCurrentPeriod && availableCategories.length > 0 && (
            <button onClick={() => setShowForm(!showForm)} className={`${btnPrimary} min-h-[44px] sm:min-h-0`}>
              {showForm ? "Cancel" : "+ Add Budget"}
            </button>
          )}
        </div>
      </div>

      {/* Period navigation */}
      {periods.length > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-3">
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

      {!isCurrentPeriod && periods.length > 0 && (
        <div className="mb-5 rounded-md border border-border-subtle bg-surface-raised px-4 py-3 text-sm text-text-secondary">
          This period is closed — read-only.
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {showForm && isCurrentPeriod && (
        <div className={`mb-6 ${card} p-6`}>
          <form onSubmit={handleAdd} className="flex flex-col gap-4 sm:flex-row sm:flex-wrap sm:items-end">
            <div className="w-full sm:flex-1 sm:min-w-[200px]">
              <label htmlFor="b-cat" className={label}>Category</label>
              <select id="b-cat" required value={formCategoryId} onChange={(e) => setFormCategoryId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                <option value="">Select category</option>
                {availableCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
            <div className="w-full sm:w-40">
              <label htmlFor="b-amount" className={label}>Monthly limit</label>
              <input id="b-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <button type="submit" className={`${btnPrimary} min-h-[44px] sm:min-h-0`}>Add</button>
          </form>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {/* Summary */}
          {budgets.length > 0 && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
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
              <div className="p-4" style={{ height: Math.max(budgets.length * 36, 100) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={budgets.map((b) => ({
                    name: b.category_name,
                    spent: Number(b.spent),
                    remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
                    over: Math.max(Number(b.spent) - Number(b.amount), 0),
                  }))} layout="vertical" margin={{ left: 0, right: 0, top: 0, bottom: 0 }}>
                    <XAxis type="number" hide />
                    <YAxis type="category" dataKey="name" width={100} tick={{ fill: "#9ba8bd", fontSize: 11 }} />
                    <Tooltip
                      formatter={(v, name) => [
                        formatAmount(Number(v)),
                        name === "spent" ? <span style={{ color: "#f87171" }}>Spent</span>
                          : name === "over" ? <span style={{ color: "#f87171" }}>Over budget</span>
                          : <span style={{ color: "#4ade80" }}>Remaining</span>,
                      ]}
                      contentStyle={{ fontSize: "11px" }}
                    />
                    <Bar dataKey="spent" stackId="a" radius={[4, 0, 0, 4]} animationDuration={600}
                      cursor="pointer"
                      onClick={(data) => {
                        const name = data?.name || data?.payload?.name;
                        if (name) router.push(`/transactions?category=${encodeURIComponent(name)}`);
                      }}
                    >
                      {budgets.map((b, i) => (
                        <Cell key={i} fill={b.percent_used > 100 ? "#f87171" : b.percent_used > 80 ? "#f59e0b" : "#D4A64A"} />
                      ))}
                    </Bar>
                    <Bar dataKey="remaining" stackId="a" fill="#e8ebf0" radius={[0, 4, 4, 0]} animationDuration={600} />
                    <Bar dataKey="over" stackId="a" fill="#f87171" radius={[0, 4, 4, 0]} animationDuration={600} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-3 flex gap-4 px-4 pb-2 text-[10px] text-text-muted">
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#D4A64A" }} /> Spent</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#f59e0b" }} /> &gt;80%</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#f87171" }} /> Over budget</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "#e8ebf0" }} /> Remaining</span>
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
                const transferTargets = masterCategories.filter((c) => c.id !== b.category_id);
                return (
                  <div key={b.id} className="px-6 py-3">
                    {editingId === b.id && isCurrentPeriod ? (
                      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
                        <span className="text-sm font-medium text-text-primary sm:flex-1">{b.category_name}</span>
                        <input type="number" step="0.01" min="0.01" value={editAmount} onChange={(e) => setEditAmount(e.target.value)}
                          className={`w-full sm:w-32 ${input}`} autoFocus
                          onKeyDown={(e) => { if (e.key === "Enter") handleUpdate(b.id); if (e.key === "Escape") setEditingId(null); }} />
                        <div className="flex flex-wrap gap-2">
                          <button onClick={() => handleUpdate(b.id)} className="min-h-[44px] text-xs text-accent hover:text-accent-hover sm:min-h-0">Save</button>
                          <button onClick={() => setEditingId(null)} className="min-h-[44px] text-xs text-text-muted hover:text-text-secondary sm:min-h-0">Cancel</button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                          <div className="flex items-center">
                            <span className="text-sm text-text-primary">{b.category_name}</span>
                            <span className={`ml-auto text-sm tabular-nums md:hidden ${overBudget ? "text-danger font-medium" : "text-text-secondary"}`}>
                              {formatAmount(b.spent)} / {formatAmount(b.amount)}
                            </span>
                          </div>
                          <div className="flex flex-wrap items-center gap-2 md:gap-4">
                            <span className={`hidden text-sm tabular-nums md:inline ${overBudget ? "text-danger font-medium" : "text-text-secondary"}`}>
                              {formatAmount(b.spent)} / {formatAmount(b.amount)}
                            </span>
                            <span className={`text-xs tabular-nums ${overBudget ? "text-danger" : "text-text-muted"}`}>
                              {b.percent_used}%
                            </span>
                            {isCurrentPeriod && (
                              <div className="flex flex-wrap gap-2 ml-auto md:ml-0">
                                <button onClick={() => { setTransferringId(transferringId === b.id ? null : b.id); setTransferCategoryId(""); setTransferAmount(""); }} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">Transfer</button>
                                <button onClick={() => { setEditingId(b.id); setEditAmount(String(b.amount)); }} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">Edit</button>
                                <button onClick={() => setConfirmDeleteId(b.id)} className="min-h-[44px] text-xs text-text-muted hover:text-danger md:min-h-0">Remove</button>
                              </div>
                            )}
                          </div>
                        </div>
                        {transferringId === b.id && isCurrentPeriod && (
                          <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
                            <select value={transferCategoryId} onChange={(e) => setTransferCategoryId(e.target.value === "" ? "" : Number(e.target.value))} className={`w-full min-w-0 sm:flex-1 sm:basis-40 ${input}`}>
                              <option value="">Select target category</option>
                              {transferTargets.map((c) => <option key={c.id} value={c.id}>{c.name}{budgetedCatIds.has(c.id) ? " (has budget)" : ""}</option>)}
                            </select>
                            <input type="number" step="0.01" min="0.01" max={Number(b.amount)} placeholder="Amount" value={transferAmount} onChange={(e) => setTransferAmount(e.target.value)}
                              className={`w-full sm:w-28 ${input}`}
                              onKeyDown={(e) => { if (e.key === "Enter" && transferCategoryId && transferAmount) handleTransfer(b.id); if (e.key === "Escape") setTransferringId(null); }} />
                            <div className="flex flex-wrap gap-2">
                              <button onClick={() => handleTransfer(b.id)} disabled={!transferCategoryId || !transferAmount} className="min-h-[44px] text-xs text-accent hover:text-accent-hover disabled:opacity-50 sm:min-h-0">Transfer</button>
                              <button onClick={() => setTransferringId(null)} className="min-h-[44px] text-xs text-text-muted hover:text-text-secondary sm:min-h-0">Cancel</button>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                );
              })}
              {budgets.length === 0 && (
                <div className="px-6 py-8 text-center text-sm text-text-muted">
                  {isCurrentPeriod
                    ? <>No budgets set. Click &quot;+ Add Budget&quot; to allocate spending limits for your categories.</>
                    : <>No budgets were set for this period.</>
                  }
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      <ConfirmModal
        open={isCurrentPeriod && confirmDeleteId !== null}
        title="Remove Budget"
        message="Remove this budget? This cannot be undone."
        confirmLabel="Remove"
        variant="danger"
        onConfirm={() => confirmDeleteId !== null && handleDelete(confirmDeleteId)}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </AppShell>
  );
}
