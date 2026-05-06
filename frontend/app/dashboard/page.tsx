"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount, formatLocalDate, projectedPeriodEnd, todayISO } from "@/lib/format";
import { input, label, btnPrimary, btnSecondary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";


import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, Cell, Tooltip, ResponsiveContainer } from "recharts";
import CategorySelect from "@/components/ui/CategorySelect";
import OnTrackTile from "@/components/dashboard/OnTrackTile";
import type { Account, BillingPeriod, Budget, Category, Transaction } from "@/lib/types";

interface ForecastPlanItem {
  id: number;
  plan_id: number;
  category_id: number;
  category_name: string;
  parent_id: number | null;
  type: "income" | "expense";
  planned_amount: string;
  source: "manual" | "recurring" | "history";
  actual_amount: string;
  variance: string;
}

interface ForecastPlan {
  id: number;
  billing_period_id: number;
  period_start: string;
  period_end: string | null;
  status: "draft" | "active";
  total_planned_income: string;
  total_planned_expense: string;
  total_actual_income: string;
  total_actual_expense: string;
  items: ForecastPlanItem[];
}

// Shape returned by GET /api/v1/forecast?period_start=...
// Generated server-side by backend/app/services/forecast_service.py.
// Only the fields the OnTrackTile reads are typed strictly; the rest
// (per-category breakdown, individual line items) we leave as unknown
// because nothing on the dashboard surface uses them today.
interface ForecastProjection {
  period_start: string;
  period_end: string;
  executed_income: string;
  executed_expense: string;
  executed_net: string;
  pending_income: string;
  pending_expense: string;
  recurring_income: string;
  recurring_expense: string;
  forecast_income: string;
  forecast_expense: string;
  forecast_net: string;
  categories: unknown[];
}

const PAGE_SIZE = 10;

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [resetBanner, setResetBanner] = useState(false);

  // L3.1: read ?reset=1 left by the data-reset flow, show a one-time
  // success banner, then strip the param so a refresh doesn't replay it.
  // Reads window.location instead of useSearchParams() so /dashboard
  // can stay statically prerenderable in Next 15 — useSearchParams
  // would force a Suspense boundary or a deopt warning at build time,
  // and this banner is purely a client-only artifact.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("reset") === "1") {
      setResetBanner(true);
      router.replace("/dashboard");
    }
  }, [router]);

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [allTransactions, setAllTransactions] = useState<Transaction[]>([]);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [period, setPeriod] = useState<BillingPeriod | null>(null);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [billingCycleDay, setBillingCycleDay] = useState(user?.billing_cycle_day ?? 1);
  const [periodIdx, setPeriodIdx] = useState(0);
  const [forecast, setForecast] = useState<ForecastPlan | null>(null);
  const [forecastProjection, setForecastProjection] = useState<ForecastProjection | null>(null);
  const [projectionFailed, setProjectionFailed] = useState(false);
  const [projectionLoading, setProjectionLoading] = useState(false);
  // Monotonically-increasing request id for the projection fetch. Used
  // to discard stale responses when a newer fetch has already started
  // (e.g. period nav during an in-flight call, or two writes in quick
  // succession). Only the latest in-flight request is allowed to
  // commit projection state.
  const projectionRequestId = useRef(0);
  const [fetching, setFetching] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");

  // Quick-add form
  const [showForm, setShowForm] = useState(false);
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
  const [formTransferCatId, setFormTransferCatId] = useState<number | "">("");

  const [chartFilter, setChartFilter] = useState<string | null>(null);
  const [dashSortField, setDashSortField] = useState<"date" | "description" | "amount">("date");
  const [dashSortDir, setDashSortDir] = useState<"asc" | "desc">("desc");

  // Selected period (navigate with arrows). On first paint before
  // loadRefs() finishes this is null — distinguish that from "we have a
  // real period" so we don't query period-scoped endpoints with a
  // calendar-month fallback that the backend won't recognize.
  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : period;
  const realPeriodStart: string | null = selectedPeriod?.start_date ?? null;
  // Period-state booleans drive empty-state copy and CTAs across the
  // Forecast and Budget tiles. Current = open period (no end_date).
  // Past = closed and ended before today. Future = scheduled stub
  // whose start is still ahead. Past + future both warrant different
  // CTAs (or none) than current — same scope rule as the Budgets page.
  const _today = todayISO();
  const isCurrentSelectedPeriod = selectedPeriod?.end_date === null;
  const isPastSelectedPeriod = !!(selectedPeriod?.end_date && selectedPeriod.end_date < _today);
  const isFutureSelectedPeriod = !!(selectedPeriod && selectedPeriod.start_date > _today);
  // monthFrom drives transaction date filters (which don't go through
  // resolve_period), so the calendar fallback is fine there.
  const monthFrom = realPeriodStart ?? formatLocalDate(new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  // For open periods, compute expected end from billing cycle day
  const monthTo =
    selectedPeriod?.end_date
    ?? (monthFrom ? projectedPeriodEnd(monthFrom, billingCycleDay) ?? "" : "");

  const loadRefs = useCallback(async () => {
    const [accts, cats, bds, per, plist, bc] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
      apiFetch<Budget[]>("/api/v1/budgets"),
      apiFetch<BillingPeriod>("/api/v1/settings/billing-period"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle"),
    ]);
    setAccounts(accts ?? []);
    setCategories(cats ?? []);
    setBudgets(bds ?? []);
    if (bc) setBillingCycleDay(bc.billing_cycle_day);
    if (per) setPeriod(per);
    const pl = plist ?? [];
    setPeriods(pl);
    // Default to current period (open = no end_date), not index 0
    const currentIdx = pl.findIndex((p) => p.end_date === null);
    if (currentIdx >= 0) setPeriodIdx(currentIdx);
  }, []);

  const loadTransactions = useCallback(async (p: number) => {
    // Omit period_start until refs have loaded a real billing period.
    // /api/v1/budgets and /api/v1/forecast-plans/current both resolve to
    // the current open period when period_start is absent — and the
    // strict resolver rejects calendar-month dates that don't match a
    // BillingPeriod row (salary-cycle orgs start mid-month).
    const budgetUrl = realPeriodStart ? `/api/v1/budgets?period_start=${realPeriodStart}` : "/api/v1/budgets";
    const forecastUrl = realPeriodStart ? `/api/v1/forecast-plans/current?period_start=${realPeriodStart}` : "/api/v1/forecast-plans/current";
    const dateFilter = `date_from=${monthFrom}${monthTo ? `&date_to=${monthTo}` : ""}`;
    const [pageData, allData, bds, fc] = await Promise.all([
      apiFetch<Transaction[]>(`/api/v1/transactions?limit=${PAGE_SIZE + 1}&offset=${p * PAGE_SIZE}&${dateFilter}`),
      p === 0 ? apiFetch<Transaction[]>(`/api/v1/transactions?limit=200&${dateFilter}`) : null,
      p === 0 ? apiFetch<Budget[]>(budgetUrl) : null,
      p === 0 ? apiFetch<ForecastPlan | null>(forecastUrl) : null,
    ]);
    const page_txs = pageData ?? [];
    setHasMore(page_txs.length > PAGE_SIZE);
    setTransactions(page_txs.slice(0, PAGE_SIZE));
    if (allData) setAllTransactions(allData);
    if (bds) setBudgets(bds);
    // null is a valid response (no plan yet) — set state so empty-state UI renders.
    if (p === 0) setForecast(fc ?? null);
    setFetching(false);
  }, [monthFrom, monthTo, realPeriodStart]);

  // Loads the forecast projection from /api/v1/forecast for the
  // currently-selected billing period. Separate from loadTransactions
  // because (a) failure here should NOT crash the whole dashboard load
  // — the OnTrackTile renders a "Projection unavailable. Retry" inline
  // state instead — and (b) the user can retry from the tile without
  // re-fetching everything else.
  const loadForecastProjection = useCallback(async () => {
    if (!realPeriodStart) {
      // Bump the id so any in-flight request from a previous period
      // can't commit state after we've cleared it.
      projectionRequestId.current += 1;
      setForecastProjection(null);
      setProjectionFailed(false);
      setProjectionLoading(false);
      return;
    }
    // Clear stale data synchronously so a period change or a
    // post-write refetch doesn't render the previous period's
    // projection while the new one is in flight.
    const myId = ++projectionRequestId.current;
    setForecastProjection(null);
    setProjectionFailed(false);
    setProjectionLoading(true);
    try {
      const projection = await apiFetch<ForecastProjection>(
        `/api/v1/forecast?period_start=${realPeriodStart}`,
      );
      // A newer request has started; this response is stale.
      if (projectionRequestId.current !== myId) return;
      setForecastProjection(projection);
      setProjectionFailed(false);
    } catch {
      if (projectionRequestId.current !== myId) return;
      setForecastProjection(null);
      setProjectionFailed(true);
    } finally {
      if (projectionRequestId.current === myId) {
        setProjectionLoading(false);
      }
    }
  }, [realPeriodStart]);

  useEffect(() => {
    if (!loading && user) {
      // Previously `.catch(() => {})` — any failure here (backend 500,
      // network blip) left the dashboard with stale or missing
      // reference data and no visible error, the user's only clue
      // being widgets that silently fail to populate. Surface it
      // through the existing error banner instead.
      loadRefs().catch((err) => {
        setError(extractErrorMessage(err, "Failed to load dashboard data"));
      });
    }
  }, [loading, user, loadRefs]);

  useEffect(() => {
    // Gate the period-scoped load on a real billing period being in
    // state. Two reasons: (a) the pre-refs request would race the real
    // one and could overwrite transactions/forecast/budgets state with
    // a calendar-fallback window if it resolved out of order; (b) it
    // would always fail anyway against the strict resolve_period for
    // salary-cycle orgs whose period doesn't start on the 1st.
    if (!loading && user && realPeriodStart) {
      setFetching(true);
      // Same class of bug as the loadRefs catch above: a failed
      // transaction fetch used to clear the spinner and vanish. Now
      // the error surfaces alongside the rest of the load failures.
      loadTransactions(page).catch((err) => {
        setError(extractErrorMessage(err, "Failed to load transactions"));
        setFetching(false);
      });
    }
  }, [loading, user, loadTransactions, page, realPeriodStart]);

  useEffect(() => {
    if (!loading && user && realPeriodStart) {
      void loadForecastProjection();
    }
  }, [loading, user, realPeriodStart, loadForecastProjection]);

  function handleTypeChange(t: "income" | "expense") {
    setFormType(t);
    setFormCategoryId("");
  }

  async function handleQuickAdd(e: FormEvent) {
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
            ...(formTransferCatId !== "" ? { category_id: formTransferCatId } : {}),
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
      setFormTransferCatId("");
      setFormRecurring(false);
      setFormAutoSettle(false);
      setFormDate(todayISO());
      setShowForm(false);
      await loadRefs();
      await loadTransactions(0);
      // The hero verdict is computed from the projection's executed_expense
      // and forecast_expense. After a write those numbers are stale until
      // we re-call /api/v1/forecast, otherwise the page's primary answer
      // ("are we on track?") can be wrong.
      void loadForecastProjection();
    } catch (err) {
      setError(extractErrorMessage(err));
    }
  }

  const activeAccounts = accounts.filter((a) => a.is_active);
  const defaultAccount = activeAccounts.find((a) => a.is_default);
  const canAdd = activeAccounts.length > 0 && categories.length > 0;

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

  // Total balance by currency (settled only — what's in the accounts)
  const balanceByCurrency = activeAccounts.reduce<Record<string, number>>(
    (acc, a) => {
      const cur = a.currency || "EUR";
      acc[cur] = (acc[cur] || 0) + Number(a.balance);
      return acc;
    },
    {}
  );
  const currencies = Object.entries(balanceByCurrency);

  // All active accounts for individual tiles
  const accountsWithBalance = activeAccounts;

  // Precompute tx map for O(1) linked lookups
  const txMap = new Map(allTransactions.map((tx) => [tx.id, tx]));

  // Pending totals per account from all period transactions
  const pendingByAccount = allTransactions
    .filter((tx) => tx.status === "pending")
    .reduce<Record<number, number>>((acc, tx) => {
      const sign = tx.type === "income" ? 1 : -1;
      acc[tx.account_id] = (acc[tx.account_id] || 0) + Number(tx.amount) * sign;
      return acc;
    }, {});

  // Spending by category from all period transactions. Transfer expense
  // halves carry linked_transaction_id; excluding them here stops transfers
  // from polluting the Spending by Category donut.
  const spendingByCategory = allTransactions
    .filter((tx) => tx.type === "expense" && tx.status === "settled" && tx.linked_transaction_id == null)
    .reduce<Record<string, number>>((acc, tx) => {
      acc[tx.category_name] = (acc[tx.category_name] || 0) + Number(tx.amount);
      return acc;
    }, {});
  const donutData = Object.entries(spendingByCategory)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value);

  // When chart filter is active, show from allTransactions; otherwise paginated
  const txSource = chartFilter ? allTransactions : transactions;

  // Dedup transfers
  const hiddenIds = new Set<number>();
  for (const tx of txSource) {
    if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) hiddenIds.add(tx.id);
  }
  const visibleTxs = txSource.filter((tx) => !hiddenIds.has(tx.id));


  function toggleDashSort(field: typeof dashSortField) {
    if (dashSortField === field) setDashSortDir(dashSortDir === "asc" ? "desc" : "asc");
    else { setDashSortField(field); setDashSortDir(field === "date" ? "desc" : "asc"); }
  }

  // Sort + filter the visible transactions
  const sortedVisibleTxs = visibleTxs
    .filter((tx) => !chartFilter || tx.category_name === chartFilter)
    .sort((a, b) => {
      let cmp = 0;
      if (dashSortField === "date") cmp = a.date.localeCompare(b.date);
      else if (dashSortField === "description") cmp = a.description.localeCompare(b.description);
      else if (dashSortField === "amount") cmp = Number(a.amount) - Number(b.amount);
      return dashSortDir === "asc" ? cmp : -cmp;
    });

  const CHART_COLORS = [
    "var(--color-chart-1)",
    "var(--color-chart-2)",
    "var(--color-chart-3)",
    "var(--color-chart-4)",
    "var(--color-chart-5)",
  ];

  return (
    <AppShell>
      <div className="mb-6 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
        <div className="flex items-center gap-2">
          {canAdd && (
            <button onClick={() => setShowForm(!showForm)} className={btnPrimary}>
              {showForm ? "Cancel" : "+ Quick Add"}
            </button>
          )}
          <Link href="/import" className={btnSecondary}>
            Import
          </Link>
        </div>
      </div>

      {resetBanner && (
        <div
          data-testid="reset-banner"
          className="mb-4 flex items-start justify-between gap-3 rounded-md border border-success/40 bg-success-dim p-4"
        >
          <div className="text-sm text-text-primary">
            <strong>Your data has been reset.</strong> Welcome back to a clean slate.
          </div>
          <button
            type="button"
            onClick={() => setResetBanner(false)}
            aria-label="Dismiss"
            className="text-lg leading-none text-text-secondary hover:text-text-primary"
          >
            ×
          </button>
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-5">
          {/* Quick-add form */}
          {showForm && (
            <div className={`${card} p-6`}>
              <div className="mb-4 flex items-center gap-4">
                <h2 className={cardTitle}>{formMode === "transfer" ? "Quick Transfer" : "Quick Add"}</h2>
                <div className="flex rounded-md border border-border text-xs">
                  <button type="button" onClick={() => setFormMode("transaction")} className={`px-3 py-1 rounded-l-md ${formMode === "transaction" ? "bg-surface-overlay text-text-primary" : "text-text-muted hover:bg-surface-raised"}`}>Transaction</button>
                  <button type="button" onClick={() => setFormMode("transfer")} className={`px-3 py-1 rounded-r-md ${formMode === "transfer" ? "bg-surface-overlay text-text-primary" : "text-text-muted hover:bg-surface-raised"}`}>Transfer</button>
                </div>
              </div>
              <form onSubmit={handleQuickAdd} className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div>
                  <label htmlFor="da-account" className={label}>{formMode === "transfer" ? "From" : "Account"}</label>
                  <select id="da-account" required value={formAccountId} onChange={(e) => handleAccountChange(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                    <option value="">Select account</option>
                    {activeAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </div>
                {formMode === "transfer" ? (
                  <div>
                    <label htmlFor="da-to-account" className={label}>To</label>
                    <select id="da-to-account" required value={formToAccountId} onChange={(e) => setFormToAccountId(e.target.value === "" ? "" : Number(e.target.value))} className={input}>
                      <option value="">Select account</option>
                      {activeAccounts.filter((a) => a.id !== formAccountId).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  </div>
                ) : (
                  <div>
                    <label htmlFor="da-type" className={label}>Type</label>
                    <select id="da-type" value={formType} onChange={(e) => handleTypeChange(e.target.value as "income" | "expense")} className={input}>
                      <option value="expense">Expense</option>
                      <option value="income">Income</option>
                    </select>
                  </div>
                )}
                {formMode === "transaction" && (
                  <div>
                    <label htmlFor="da-category" className={label}>Category</label>
                    <CategorySelect id="da-category" categories={categories} value={formCategoryId} onChange={setFormCategoryId} filterType={formType} className={input} />
                  </div>
                )}
                {formMode === "transfer" && (
                  <div>
                    <label className={label}>Category (optional)</label>
                    <CategorySelect
                      id="da-transfer-cat"
                      categories={categories}
                      value={formTransferCatId}
                      onChange={setFormTransferCatId}
                      className={input}
                    />
                    <p className="mt-1 text-[10px] text-text-muted">Defaults to Transfer. Override to track in budgets.</p>
                  </div>
                )}
                <div>
                  <label htmlFor="da-desc" className={label}>Description</label>
                  <input id="da-desc" type="text" required={formMode === "transaction"} placeholder={formMode === "transfer" ? "Auto: Transfer from X to Y" : "What was it for?"} value={formDescription} onChange={(e) => setFormDescription(e.target.value)} className={input} />
                </div>
                <div>
                  <label htmlFor="da-amount" className={label}>Amount</label>
                  <input id="da-amount" type="number" step="0.01" min="0.01" required placeholder="0.00" value={formAmount} onChange={(e) => setFormAmount(e.target.value)} className={input} />
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
                {formMode === "transaction" ? (
                  <div className="flex items-end gap-3">
                    <label className="flex items-center gap-2 text-sm text-text-secondary">
                      <input type="checkbox" checked={formRecurring} onChange={(e) => setFormRecurring(e.target.checked)} className="rounded border-border" />
                      Repeats
                    </label>
                    {formRecurring && (
                      <select value={formFrequency} onChange={(e) => setFormFrequency(e.target.value)} aria-label="Frequency" className={`w-28 text-xs ${input}`}>
                        <option value="monthly">Monthly</option>
                        <option value="weekly">Weekly</option>
                        <option value="yearly">Yearly</option>
                      </select>
                    )}
                  </div>
                ) : (
                  <div className="flex items-end">
                    <button type="submit" className={btnPrimary}>Transfer</button>
                  </div>
                )}
                {formMode === "transaction" && (
                  <div className="flex items-end">
                    <button type="submit" className={btnPrimary}>Add</button>
                  </div>
                )}
              </form>
            </div>
          )}

          {/* ═══ BILLING PERIOD — standalone nav bar ═══ */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button onClick={() => { setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1)); setChartFilter(null); }} disabled={periodIdx >= periods.length - 1} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Previous period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
              </button>
              <span className="text-sm font-medium text-text-primary">
                {monthFrom}{monthTo ? ` — ${monthTo}` : ""}
              </span>
              <button onClick={() => { setPeriodIdx(Math.max(periodIdx - 1, 0)); setChartFilter(null); }} disabled={periodIdx <= 0} className="rounded p-1 text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Next period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
              </button>
              {selectedPeriod?.end_date === null && <span className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success">CURRENT</span>}
              {selectedPeriod?.end_date !== null && (
                <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) { setPeriodIdx(idx); setChartFilter(null); } }} className="ml-1 rounded-md px-2 py-1 text-[11px] font-medium text-text-muted hover:bg-surface-raised">Today</button>
              )}
            </div>
            <Link href="/transactions" className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary">View All Transactions</Link>
          </div>

          {/* ═══ ROW 1: On Track hero — single full-width tile ═══ */}
          <OnTrackTile
            forecastPlan={forecast}
            projection={forecastProjection}
            projectionFailed={projectionFailed}
            projectionLoading={projectionLoading}
            onRetryProjection={() => void loadForecastProjection()}
            isPastPeriod={isPastSelectedPeriod}
            isFuturePeriod={isFutureSelectedPeriod}
          />

          {/* ═══ ROW 2: Accounts — single row, primary slightly bigger ═══ */}
          {accountsWithBalance.length > 0 && (() => {
            const defaultAcct = accountsWithBalance.find((a) => a.is_default);
            const others = accountsWithBalance.filter((a) => !a.is_default);
            return (
              <div className="grid grid-cols-1 gap-3 sm:flex sm:gap-3 sm:overflow-x-auto sm:pb-1">
                {/* Primary account — wider */}
                {defaultAcct && (
                  <div className={`${card} px-5 py-3 sm:shrink-0 sm:min-w-[220px]`}>
                    <div className="flex items-center gap-2">
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-primary">{defaultAcct.name}</p>
                      <span className="rounded border border-border px-1.5 py-0.5 text-[9px] font-semibold text-text-secondary">PRIMARY</span>
                    </div>
                    <p className="mt-1 text-xl font-semibold tabular-nums text-text-primary">{formatAmount(defaultAcct.balance)} <span className="text-xs text-text-muted">{defaultAcct.currency}</span></p>
                    {pendingByAccount[defaultAcct.id] !== undefined && pendingByAccount[defaultAcct.id] !== 0 && (
                      <p className="text-[10px] tabular-nums text-text-muted">Pending: {formatAmount(Math.abs(pendingByAccount[defaultAcct.id]))}</p>
                    )}
                  </div>
                )}
                {/* Other accounts */}
                {others.map((acct) => {
                  const pending = pendingByAccount[acct.id] || 0;
                  const isCreditCard = acct.account_type_slug === "credit_card";
                  return (
                    <div key={acct.id} className={`${card} px-4 py-3 sm:shrink-0 sm:min-w-[150px]`}>
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-text-muted truncate">{acct.name}</p>
                      <p className="mt-1 text-base font-semibold tabular-nums text-text-primary">{formatAmount(acct.balance)}</p>
                      {isCreditCard && pending !== 0 && (
                        <p className="text-[10px] tabular-nums text-danger">Pending: {formatAmount(Math.abs(pending))}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })()}

          {/* ═══ ROW 3: Three equal charts ═══ */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* Spending by category (donut) */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Spending by Category</h2>
              {chartFilter && (
                <button onClick={() => setChartFilter(null)} className="mb-2 rounded-md bg-surface-overlay px-2.5 py-1 text-xs text-text-secondary hover:bg-surface-raised">
                  Filtering: {chartFilter} &times;
                </button>
              )}
              {donutData.length > 0 ? (
                <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
                  <div className="h-40 w-40 shrink-0">
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={donutData} cx="50%" cy="50%" innerRadius={35} outerRadius={65}
                          paddingAngle={2} dataKey="value" stroke="none" cursor="pointer"
                          onClick={(_, idx) => {
                            const name = donutData[idx]?.name;
                            setChartFilter(chartFilter === name ? null : name);
                          }}
                        >
                          {donutData.map((d, i) => (
                            <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]}
                              opacity={chartFilter && chartFilter !== d.name ? 0.3 : 1} />
                          ))}
                        </Pie>
                        <Tooltip formatter={(v) => formatAmount(Number(v))} contentStyle={{ fontSize: "12px" }} />
                      </PieChart>
                    </ResponsiveContainer>
                  </div>
                  <div className="w-full space-y-1.5 sm:flex-1">
                    {donutData.slice(0, 10).map((d, i) => (
                      <button key={d.name} onClick={() => setChartFilter(chartFilter === d.name ? null : d.name)}
                        className={`flex w-full items-center justify-between rounded px-1.5 py-0.5 transition-colors hover:bg-surface-raised ${chartFilter === d.name ? "bg-surface-overlay" : ""}`}>
                        <div className="flex items-center gap-2">
                          <div className="h-2.5 w-2.5 rounded-full" style={{ background: CHART_COLORS[i % CHART_COLORS.length] }} />
                          <span className="text-xs text-text-secondary">{d.name}</span>
                        </div>
                        <span className="text-xs tabular-nums text-text-muted">{formatAmount(d.value)}</span>
                      </button>
                    ))}
                    {donutData.length > 10 && (
                      <p className="px-1.5 text-[10px] text-text-muted">+{donutData.length - 10} more — click chart to filter</p>
                    )}
                  </div>
                </div>
              ) : (
                <p className="text-sm text-text-muted py-6 text-center">No expense data yet</p>
              )}
            </div>

            {/* Budget progress */}
            <div className={`${card} overflow-hidden`}>
              <div className={`flex items-center justify-between ${cardHeader}`}>
                <h2 className={cardTitle}>Budget Progress</h2>
                <Link href="/budgets" className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary">Manage</Link>
              </div>
              {budgets.length > 0 ? (
                <>
                <div className="p-4" style={{ height: Math.max(budgets.slice(0, 6).length * 40, 100) }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={budgets.slice(0, 6).map((b) => ({
                      name: b.category_name,
                      spent: Number(b.spent),
                      remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
                      pct: b.percent_used,
                    }))} layout="vertical" margin={{ left: 0, right: 20, top: 0, bottom: 0 }}>
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="name" width={100} tick={{ fill: "var(--color-text-secondary)", fontSize: 11 }} />
                      <Tooltip
                        formatter={(v, name) => [
                          formatAmount(Number(v)),
                          name === "spent" ? <span style={{ color: "var(--color-chart-5)" }}>Spent</span> : <span style={{ color: "var(--color-chart-2)" }}>Remaining</span>,
                        ]}
                        contentStyle={{ fontSize: "11px" }}
                      />
                      <Bar dataKey="spent" stackId="a" radius={[4, 0, 0, 4]} animationDuration={600}
                        cursor="pointer"
                        onClick={(_, idx) => {
                          const name = budgets.slice(0, 6)[idx]?.category_name;
                          if (name) setChartFilter(chartFilter === name ? null : name);
                        }}
                      >
                        {budgets.slice(0, 6).map((b, i) => (
                          <Cell key={i} fill={b.percent_used > 100 ? "var(--color-chart-5)" : b.percent_used > 80 ? "var(--color-chart-4)" : "var(--color-chart-1)"} />
                        ))}
                      </Bar>
                      <Bar dataKey="remaining" stackId="a" fill="var(--color-border)" radius={[0, 4, 4, 0]} animationDuration={600} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <div className="flex flex-wrap gap-3 px-4 pb-3 text-[10px] text-text-muted">
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-1)" }} /> Spent</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-4)" }} /> &gt;80%</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-5)" }} /> Over budget</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-border)" }} /> Remaining</span>
                </div>
                </>
              ) : (
                <div className="px-5 py-6 text-center text-sm text-text-muted">
                  {isPastSelectedPeriod
                    ? <>No budgets were set for this period.</>
                    : isFutureSelectedPeriod
                      ? <>Future budgets live in Forecasts. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Plan ahead →</Link></>
                      : <>No budgets for this period. <Link href="/budgets" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Add one</Link></>
                  }
                </div>
              )}
            </div>

            {/* Forecast comparison — planned vs actual per category */}
            <div className={`${card} overflow-hidden p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Forecast by Category</h2>
              {(() => {
                const expenseItems = forecast?.items.filter((it) => it.type === "expense") ?? [];
                if (forecast && expenseItems.length > 0) {
                  return (
                    <div style={{ height: Math.max(Math.min(expenseItems.length, 8) * 32, 100) }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={expenseItems.slice(0, 8).map((it) => ({
                            name: it.category_name.length > 12 ? it.category_name.slice(0, 12) + "…" : it.category_name,
                            planned: Number(it.planned_amount),
                            actual: Number(it.actual_amount),
                          }))}
                          layout="vertical"
                          margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
                        >
                          <XAxis type="number" hide />
                          <YAxis type="category" dataKey="name" width={90} tick={{ fill: "var(--color-text-secondary)", fontSize: 10 }} />
                          <Tooltip
                            formatter={(v, name) => [
                              formatAmount(Number(v)),
                              name === "planned" ? <span style={{ color: "var(--color-chart-1)" }}>Planned</span> : <span style={{ color: "var(--color-chart-2)" }}>Actual</span>,
                            ]}
                            contentStyle={{ fontSize: "11px" }}
                          />
                          <Bar dataKey="planned" fill="var(--color-chart-1)" radius={[4, 4, 4, 4]} animationDuration={600}
                            cursor="pointer"
                            onClick={(_, idx) => {
                              const name = expenseItems[idx]?.category_name;
                              if (name) setChartFilter(chartFilter === name ? null : name);
                            }}
                          />
                          <Bar dataKey="actual" fill="var(--color-chart-2)" radius={[4, 4, 4, 4]} animationDuration={600}>
                            {expenseItems.slice(0, 8).map((it, i) => (
                              <Cell key={i} fill={Number(it.actual_amount) > Number(it.planned_amount) ? "var(--color-chart-5)" : "var(--color-chart-2)"} />
                            ))}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  );
                }
                return (
                  <p className="text-sm text-text-muted py-6 text-center">
                    {isPastSelectedPeriod
                      ? <>No forecast was set for this period.</>
                      : isFutureSelectedPeriod
                        ? <>No forecast for this future period. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Plan ahead</Link>.</>
                        : <>No forecast for this period. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Set one up</Link>.</>
                    }
                  </p>
                );
              })()}
              <div className="mt-2 flex gap-3 text-[10px] text-text-muted">
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-1)" }} /> Planned</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-2)" }} /> Under plan</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--color-chart-5)" }} /> Over plan</span>
              </div>
            </div>
          </div>

          {/* Recent transactions */}
          <div className={card}>
            <div className={`flex items-center justify-between ${cardHeader}`}>
              <h2 className={cardTitle}>Recent Transactions</h2>
            </div>
            {/* Sortable mini-header */}
            <div className="flex items-center justify-between px-5 py-1.5 border-b border-border-subtle text-[10px] font-semibold uppercase tracking-wider text-text-muted">
              <div className="flex items-center gap-3">
                <button onClick={() => toggleDashSort("date")} className="w-16 text-left hover:text-text-primary">Date{dashSortField === "date" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
                <button onClick={() => toggleDashSort("description")} className="text-left hover:text-text-primary">Description{dashSortField === "description" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
              </div>
              <button onClick={() => toggleDashSort("amount")} className="hover:text-text-primary">Amount{dashSortField === "amount" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
            </div>
            <div className="divide-y divide-border-subtle">
              {sortedVisibleTxs.map((tx) => {
                const isTransfer = tx.linked_transaction_id !== null;
                const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                return (
                  <div key={tx.id} className="flex items-center justify-between px-5 py-2.5">
                    <div className="flex items-center gap-3 min-w-0">
                      <span className="text-xs tabular-nums text-text-muted w-16 shrink-0">{tx.date.slice(5)}</span>
                      <div className="min-w-0">
                        <p className="text-sm text-text-primary truncate">{tx.description}</p>
                        <p className="text-[11px] text-text-muted truncate">
                          {isTransfer && linkedTx ? <>{tx.account_name} &rarr; {linkedTx.account_name}</> : <>{tx.account_name} · {tx.category_name}</>}
                          {tx.status === "pending" && <span className="ml-1 rounded bg-surface-overlay px-1 py-0.5 text-[9px] font-medium text-text-muted">pending</span>}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={`text-sm font-medium tabular-nums ${isTransfer ? "text-info" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                        {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                      </span>
                      {!isTransfer && (
                        <button onClick={async () => { try { await apiFetch(`/api/v1/transactions/${tx.id}`, { method: "PUT", body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }) }); await loadTransactions(page); await loadRefs(); void loadForecastProjection(); } catch (err) { setError(extractErrorMessage(err)); } }} aria-label={`Toggle status`} className={`rounded px-1 py-0.5 text-[9px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-surface-overlay text-text-muted"}`}>
                          {tx.status}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
              {transactions.length === 0 && (
                <div className="px-5 py-6 text-center text-sm text-text-muted">
                  {!canAdd ? "Create accounts and categories first." : "No transactions this period."}
                </div>
              )}
            </div>
            {!chartFilter && (page > 0 || hasMore) && (
              <div className="flex items-center justify-between border-t border-border px-5 py-2.5">
                <button onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised disabled:opacity-40">Prev</button>
                <span className="text-[11px] text-text-muted">Page {page + 1}</span>
                <button onClick={() => setPage(page + 1)} disabled={!hasMore} className="rounded-md border border-border px-2.5 py-1 text-[11px] text-text-secondary hover:bg-surface-raised disabled:opacity-40">Next</button>
              </div>
            )}
          </div>

          {activeAccounts.length === 0 && (
            <div className={`${card} p-10 text-center`}>
              <p className="text-text-secondary">No accounts yet.</p>
              <p className="mt-2 text-sm text-text-muted">
                Go to <Link href="/accounts" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Accounts</Link> to get started.
              </p>
            </div>
          )}
        </div>
      )}
    </AppShell>
  );
}
