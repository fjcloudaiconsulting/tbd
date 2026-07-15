"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import HelpTooltip from "@/components/help/HelpTooltip";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, todayISO } from "@/lib/format";
import { input, label, btnPrimary, btnSecondary, card, cardHeader, cardTitle, error as errorCls, pageTitle, badgeError } from "@/lib/styles";
import dynamic from "next/dynamic";
import type { BillingPeriod, Budget, Category } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import StatCard from "@/components/ui/StatCard";
// chartColor (theme tokens) stays for the static DOM legend swatches
// below the chart; the recharts subtree itself is code-split into
// BudgetOverviewChart and loaded via next/dynamic (ssr:false).
import { chartColor } from "@/lib/chart-colors";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";

const BudgetOverviewChart = dynamic(() => import("./BudgetOverviewChart"), {
  ssr: false,
  loading: () => (
    <div
      aria-hidden="true"
      className="h-full w-full animate-pulse rounded bg-surface-raised"
    />
  ),
});
import { useAiStatus } from "@/lib/hooks/use-ai-status";
import { SetUpAiCta } from "@/components/ai/SetUpAiCta";
import BudgetRebalanceModal from "@/components/budgets/BudgetRebalanceModal";
import BudgetDraftModal from "@/components/budgets/BudgetDraftModal";

export default function BudgetsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [periodIdx, setPeriodIdx] = useState(0);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");
  // Non-blocking refresh-error state for the AppShell post-write event
  // listener. The page keeps the previous list; banner offers a Retry.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
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

  // LAI.3 — Smart Budget Rebalance. The modal lazy-fetches when opened
  // and never auto-applies; user accept/skip per row, then Apply
  // writes via existing PUT /budgets/{id}.
  const [rebalanceOpen, setRebalanceOpen] = useState(false);
  // Next-period AI draft (projection-only; applies by CREATING budgets).
  const [draftOpen, setDraftOpen] = useState(false);

  const ai = useAiStatus();
  const budgetAi = ai?.budget;
  const role = user?.role ?? null;

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : null;
  const periodStart = selectedPeriod?.start_date ?? "";
  const isCurrentPeriod = selectedPeriod?.end_date === null;
  // A future stub (start_date after today) is the editable "next" period.
  const isNextPeriod = selectedPeriod
    ? selectedPeriod.start_date > todayISO()
    : false;
  // Current + next are editable; past (closed) periods are read-only.
  const isEditable = isCurrentPeriod || isNextPeriod;

  const loadRefs = useCallback(async () => {
    // Materialize the immediate next-period stub so it can be budgeted.
    // ensure-future is admin-only (mirrors the Forecasts page), so only
    // fire it for users who can actually run it — a member would just get
    // a swallowed 403. Members still see the next period once an admin (or
    // a period close) has created the stub.
    const canManagePeriods =
      !!user &&
      (user.role === "owner" || user.role === "admin" || user.is_superadmin);
    if (canManagePeriods) {
      await apiFetch("/api/v1/settings/billing-periods/ensure-future?count=1", {
        method: "POST",
      }).catch(() => {});
    }
    const [c, p] = await Promise.all([
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
    ]);
    setCategories(c ?? []);
    // Show current + past periods, plus the single nearest FUTURE stub as
    // the "next" period (design: current + next only, not multi-period).
    const today = todayISO();
    const past = (p ?? []).filter((bp) => bp.start_date <= today);
    const future = (p ?? [])
      .filter((bp) => bp.start_date > today)
      .sort((a, b) => a.start_date.localeCompare(b.start_date));
    // Prepend the next stub so index order stays newest-first (nav relies
    // on it): [next, current, prev, ...].
    const pl = future.length > 0 ? [future[0], ...past] : past;
    setPeriods(pl);
    // Default to the current period (open = no end_date), not the next one.
    const currentIdx = pl.findIndex((bp) => bp.end_date === null);
    if (currentIdx >= 0) setPeriodIdx(currentIdx);
  }, [user]);

  const loadBudgets = useCallback(async () => {
    const url = periodStart ? `/api/v1/budgets?period_start=${periodStart}` : "/api/v1/budgets";
    const b = await apiFetch<Budget[]>(url);
    setBudgets(b ?? []);
    setFetching(false);
  }, [periodStart]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial refs fetch: loadRefs() writes categories/periods into state once auth resolves
    if (!loading && user) loadRefs().catch(() => {});
  }, [loading, user, loadRefs]);

  useEffect(() => {
    if (!loading && user) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- set the fetching flag before the budgets list load kicks off
      setFetching(true);
      loadBudgets().catch(() => setFetching(false));
    }
  }, [loading, user, loadBudgets]);

  // After a write from the AppShell-level "+ New Transaction" CTA the
  // budgets page reloads its list so per-budget actuals reflect the new
  // transaction. Refs (categories/periods) don't change on a transaction
  // add. Single-call reload, plain try/catch is enough.
  const refreshAfterTransactionAdded = useCallback(async () => {
    if (loading || !user) return;
    setRefreshing(true);
    try {
      await loadBudgets();
      setRefreshError(false);
    } catch {
      setRefreshError(true);
    } finally {
      setRefreshing(false);
    }
  }, [loading, user, loadBudgets]);

  useTransactionAddedListener(() => {
    void refreshAfterTransactionAdded();
  });

  // Mutations are allowed only for editable (current + next) periods. If
  // the user navigates to a past (closed) period mid-edit, drop any open
  // form/state so they can't submit against a read-only period.
  useEffect(() => {
    if (!isEditable) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- drop open form/edit/transfer/delete state when the selected period becomes read-only
      setShowForm(false);
      setEditingId(null);
      setTransferringId(null);
      setConfirmDeleteId(null);
    }
  }, [isEditable]);

  // Master categories that don't have a budget yet
  const masterCategories = categories.filter((c) => c.parent_id === null && c.type === "expense");
  const budgetedCatIds = new Set(budgets.map((b) => b.category_id));
  const availableCategories = masterCategories.filter((c) => !budgetedCatIds.has(c.id));

  async function handleFromForecast() {
    setError("");
    try {
      const url = periodStart
        ? `/api/v1/budgets/from-forecast?period_start=${periodStart}`
        : "/api/v1/budgets/from-forecast";
      const updated = await apiFetch<Budget[]>(url, { method: "POST" });
      setBudgets(updated ?? []);
    } catch (err) {
      // Most common case: no plan exists for this period — the backend's
      // ValidationError message tells the user to create one on the
      // Forecasts page first.
      setError(extractErrorMessage(err));
    }
  }

  async function handleCopyForward() {
    setError("");
    const current = periods.find((p) => p.end_date === null);
    if (!current || !periodStart) return;
    try {
      const updated = await apiFetch<Budget[]>(
        "/api/v1/budgets/copy-from-period",
        {
          method: "POST",
          body: JSON.stringify({
            source_period_start: current.start_date,
            target_period_start: periodStart,
          }),
        },
      );
      setBudgets(updated ?? []);
    } catch (err) {
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

  // Memoize the chart data so unrelated parent renders (period nav, form
  // toggles) don't rebuild the array reference and force Recharts to
  // re-layout every bar.
  const budgetChartData = useMemo(
    () =>
      budgets.map((b) => ({
        name: b.category_name,
        spent: Number(b.spent),
        remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
        over: Math.max(Number(b.spent) - Number(b.amount), 0),
      })),
    [budgets],
  );

  return (
    <AppShell>
      <div className="mb-6 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-1" data-tour-id="budgets.title">
          <h1 className={`${pageTitle} mb-0`}>Budgets</h1>
          <HelpAnchor section="budgets" label="Budgets" />
        </div>
        <div className="flex flex-wrap gap-2">
          {isCurrentPeriod && (
            <button
              onClick={handleFromForecast}
              className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
            >
              From Forecast
            </button>
          )}
          {isCurrentPeriod && budgets.length > 0 && budgetAi?.entitled && (
            budgetAi.configured ? (
              <span className="inline-flex items-center gap-1">
                <button
                  onClick={() => setRebalanceOpen(true)}
                  className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
                  data-testid="suggest-rebalance-btn"
                >
                  Suggest rebalance
                </button>
                <HelpTooltip k="ai.budget" />
              </span>
            ) : (
              <SetUpAiCta
                role={role}
                className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
              />
            )
          )}
          {isEditable && availableCategories.length > 0 && (
            <button onClick={() => setShowForm(!showForm)} className={`${btnPrimary} sm:min-h-0`}>
              {showForm ? "Cancel" : "+ Add Budget"}
            </button>
          )}
        </div>
      </div>

      {/* Period navigation */}
      {periods.length > 0 && (
        <div className="mb-5 flex flex-wrap items-center gap-3">
          <button onClick={() => setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1))} disabled={periodIdx >= periods.length - 1} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30 md:min-h-0 md:min-w-0" aria-label="Previous period">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
          </button>
          <span className="text-sm text-text-secondary">
            {selectedPeriod?.start_date}{selectedPeriod?.end_date ? ` – ${selectedPeriod.end_date}` : ""}
            {isCurrentPeriod && <span className="ml-2 text-xs text-success font-medium">current</span>}
            {isNextPeriod && <span className="ml-2 text-xs text-accent font-medium">next</span>}
          </span>
          <button onClick={() => setPeriodIdx(Math.max(periodIdx - 1, 0))} disabled={periodIdx <= 0} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30 md:min-h-0 md:min-w-0" aria-label="Next period">
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
          </button>
          {!isCurrentPeriod && (
            <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) setPeriodIdx(idx); }} className="ml-1 rounded-md px-2 py-1 text-[11px] font-medium text-text-muted hover:bg-surface-raised">Today</button>
          )}
        </div>
      )}

      {!isEditable && periods.length > 0 && (
        <div className="mb-5 rounded-md border border-border-subtle bg-surface-raised px-4 py-3 text-sm text-text-secondary">
          This period is closed (read-only).
        </div>
      )}

      {error && (
        <div className={`mb-6 ${errorCls}`}>
          {error}
          {error.toLowerCase().includes("no forecast plan") && (
            <>
              {" "}
              <Link href="/forecast-plans" className="underline hover:no-underline">
                Go to Forecasts →
              </Link>
            </>
          )}
        </div>
      )}

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="budgets-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAfterTransactionAdded();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {showForm && isEditable && (
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
              <span className="mb-1.5 flex items-center gap-1">
                <label htmlFor="b-amount" className={`${label} mb-0`}>Monthly limit</label>
                <HelpTooltip k="budget.monthly-limit" />
              </span>
              <input id="b-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
            </div>
            <button type="submit" className={`${btnPrimary} sm:min-h-0`}>Add</button>
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
              <StatCard
                label="Total Budget"
                value={formatAmount(totalBudget)}
              />
              <StatCard
                label="Total Spent"
                value={formatAmount(totalSpent)}
                valueClassName={totalSpent > totalBudget ? "text-danger" : "text-text-primary"}
                badge={totalSpent > totalBudget ? <span className={badgeError}>Over budget</span> : undefined}
              />
              <StatCard
                label="Remaining"
                value={formatAmount(totalBudget - totalSpent)}
                valueClassName={totalBudget - totalSpent < 0 ? "text-danger" : "text-success"}
                badge={totalBudget - totalSpent < 0 ? <span className={badgeError}>Overspent</span> : undefined}
              />
            </div>
          )}

          {/* Budget chart + Details side-by-side on wide screens */}
          <div className="grid grid-cols-1 xl:grid-cols-5 gap-6">
            {/* Budget chart */}
            {budgets.length > 0 && (
              <div className={`${card} p-5 xl:col-span-3 min-w-0`}>
                <div className="flex items-center justify-between mb-4">
                  <h2 className={cardTitle}>Budget Overview</h2>
                  <span className="text-xs text-text-muted">
                    {selectedPeriod && <>{selectedPeriod.start_date}{selectedPeriod.end_date ? ` – ${selectedPeriod.end_date}` : " (open)"}</>}
                  </span>
                </div>
                <div className="w-full min-w-0 p-4" style={{ height: Math.max(budgets.length * 36, 100) }}>
                  <BudgetOverviewChart
                    budgetChartData={budgetChartData}
                    cellMeta={budgets}
                    onBarClick={(name) => {
                      if (name) router.push(`/transactions?category=${encodeURIComponent(name)}`);
                    }}
                  />
                </div>
                <div className="mt-3 flex gap-4 px-4 pb-2 text-[10px] text-text-muted">
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.spent }} /> Spent</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.watch }} /> &gt;80%</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over budget</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.remaining }} /> Remaining</span>
                </div>
              </div>
            )}

            {/* Budget details */}
            <div className={`${card} min-w-0 ${budgets.length > 0 ? "xl:col-span-2" : "xl:col-span-5"}`}>
            <div className={cardHeader}>
              <h2 className={cardTitle}>Details</h2>
            </div>
            <div className="divide-y divide-border-subtle">
              {budgets.map((b) => {
                const overBudget = b.percent_used > 100;
                const transferTargets = masterCategories.filter((c) => c.id !== b.category_id);
                return (
                  <div key={b.id} className="px-6 py-3">
                    {editingId === b.id && isEditable ? (
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
                            {isEditable && (
                              <div className="flex flex-wrap gap-2 ml-auto md:ml-0">
                                <button onClick={() => { setTransferringId(transferringId === b.id ? null : b.id); setTransferCategoryId(""); setTransferAmount(""); }} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">Transfer</button>
                                <button onClick={() => { setEditingId(b.id); setEditAmount(String(b.amount)); }} className="min-h-[44px] text-xs text-text-muted hover:text-accent md:min-h-0">Edit</button>
                                <button onClick={() => setConfirmDeleteId(b.id)} className="min-h-[44px] text-xs text-text-muted hover:text-danger md:min-h-0">Remove</button>
                              </div>
                            )}
                          </div>
                        </div>
                        {transferringId === b.id && isEditable && (
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
                isNextPeriod ? (
                  <div className="px-6 py-8 text-center" data-testid="next-period-seed">
                    <p className="mb-4 text-sm text-text-muted">
                      Get a head start on next period. Seed its budgets, then
                      fine-tune each one.
                    </p>
                    <div className="flex flex-wrap justify-center gap-2">
                      <button onClick={handleFromForecast} className={btnSecondary}>
                        From forecast
                      </button>
                      <button onClick={handleCopyForward} className={btnSecondary}>
                        Copy this period
                      </button>
                      <button
                        onClick={() => setDraftOpen(true)}
                        className={btnSecondary}
                        data-testid="ai-draft-btn"
                      >
                        AI draft from trends
                      </button>
                      <button onClick={() => setShowForm(true)} className={btnPrimary}>
                        Start blank
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="px-6 py-8 text-center text-sm text-text-muted">
                    {isCurrentPeriod
                      ? <>No budgets set. Use <strong>+ Add Budget</strong> to add one, or <strong>From Forecast</strong> to seed them from your plan.</>
                      : <>No budgets were set for this period.</>
                    }
                  </div>
                )
              )}
            </div>
            </div>
          </div>
          {/* end chart+details grid */}
        </div>
      )}
      <ConfirmModal
        open={isEditable && confirmDeleteId !== null}
        title="Remove Budget"
        message="Remove this budget? This cannot be undone."
        confirmLabel="Remove"
        variant="danger"
        onConfirm={() => confirmDeleteId !== null && handleDelete(confirmDeleteId)}
        onCancel={() => setConfirmDeleteId(null)}
      />
      <BudgetRebalanceModal
        open={rebalanceOpen}
        budgets={budgets.map((b) => ({
          id: b.id,
          category_id: b.category_id,
          amount: b.amount,
        }))}
        onApplied={() => {
          void loadBudgets();
        }}
        onClose={() => setRebalanceOpen(false)}
      />
      <BudgetDraftModal
        open={draftOpen}
        periodStart={periodStart}
        onApplied={() => {
          void loadBudgets();
        }}
        onClose={() => setDraftOpen(false)}
      />
    </AppShell>
  );
}
