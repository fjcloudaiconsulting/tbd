"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { fetchAll } from "@/lib/pagination";
import { formatAmount, formatLocalDate, projectedPeriodEnd, todayISO } from "@/lib/format";
import { btnSecondary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";


import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, Cell, Tooltip, ResponsiveContainer } from "recharts";
import { chartColor } from "@/lib/chart-colors";
import { BudgetSpentBarShape, type BudgetSpentBarShapeProps } from "@/lib/chart-shapes";
import OnTrackTile from "@/components/dashboard/OnTrackTile";
import AccountMonthEndForecast, {
  type AccountMonthEndForecastResponse,
} from "@/components/dashboard/AccountMonthEndForecast";
import AccountTilesCard from "@/components/dashboard/AccountTile";
import {
  SORT_KEY_DASHBOARD_SPENDING,
  SORT_KEY_DASHBOARD_TRANSACTIONS,
} from "@/lib/hooks/persisted-keys";
import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";
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
  // All-time pending transactions (no date filter). Pending is a status,
  // not a period concept; a CC charge from October that's still pending
  // in November must remain visible regardless of which period the user
  // is viewing. Refreshed on every write — independent of the visible
  // transaction page (the status toggle on page 2 still needs to refresh
  // the strip's pending totals).
  const [pendingTransactions, setPendingTransactions] = useState<Transaction[]>([]);
  // Counter-ref guard for the pending fetch. Two writes in quick
  // succession can issue two pending refetches; only the latest one is
  // allowed to commit state. Same pattern as projectionRequestId below.
  const pendingRequestId = useRef(0);
  const [forecastProjection, setForecastProjection] = useState<ForecastProjection | null>(null);
  const [projectionFailed, setProjectionFailed] = useState(false);
  const [projectionLoading, setProjectionLoading] = useState(false);
  // Per-account expected month-end balance from /api/v1/forecast/account-balances.
  // Distinct from forecastProjection above (which drives the OnTrackTile —
  // reportable income/expense aggregates). This one is per-account balance
  // math including pending transfer legs.
  const [accountMonthEndForecast, setAccountMonthEndForecast] =
    useState<AccountMonthEndForecastResponse | null>(null);
  // Distinguish "in flight / not yet fetched" from "load failed" so the
  // card can render an error state instead of a loading placeholder
  // forever on a 500.
  const [accountMonthEndForecastError, setAccountMonthEndForecastError] =
    useState(false);
  const accountForecastRequestId = useRef(0);
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
  // Non-blocking error from a post-write refresh. The initial-load
  // banner above (`error`) keeps its hard-fail semantics: blank page +
  // banner, no data. This one shows alongside the existing data: the
  // user keeps the previous good snapshot, sees a "Refresh failed"
  // affordance with a Retry button, and can reissue the same
  // post-write reloads without losing scroll, selection, or filters.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const [chartFilter, setChartFilter] = useState<string | null>(null);
  // Item 6 (system-wide sort persistence): the dashboard transactions table
  // and the Spending by Category card both persist their sort state via
  // localStorage so a navigate-away-and-back lands the user where they were.
  type DashTxSort = "date" | "description" | "amount";
  const dashTxSort = usePersistedSort<DashTxSort>(
    SORT_KEY_DASHBOARD_TRANSACTIONS,
    "date",
    "desc",
    ["date", "description", "amount"] as const,
  );
  const dashSortField = dashTxSort.field;
  const dashSortDir = dashTxSort.dir;
  // Item 16 (D2 sortable columns on the Spending card): name | percent |
  // amount. Default amount-desc to match the prior implicit ordering.
  type SpendingSort = "name" | "percent" | "amount";
  const spendingSort = usePersistedSort<SpendingSort>(
    SORT_KEY_DASHBOARD_SPENDING,
    "amount",
    "desc",
    ["name", "percent", "amount"] as const,
  );

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

  // All-time pending refetch. Decoupled from loadTransactions so it
  // refreshes on writes regardless of which transaction page is visible:
  // a status toggle on page 2 must still update the accounts strip.
  // Paginated through fetchAll<Transaction> so the limit=200 cap can't
  // silently drop older unresolved pending charges.
  const loadPendingTransactions = useCallback(async () => {
    const myId = ++pendingRequestId.current;
    try {
      const all = await fetchAll<Transaction>("/api/v1/transactions?status=pending");
      if (pendingRequestId.current !== myId) return;
      setPendingTransactions(all);
    } catch {
      // Pending failures are noisy but non-fatal — silently keep the
      // last good snapshot. The dashboard error banner already surfaces
      // the real problem if loadRefs / loadTransactions also failed.
    }
  }, []);

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
      // Pending is independent of period and refs; load alongside.
      void loadPendingTransactions();
    }
  }, [loading, user, loadRefs, loadPendingTransactions]);

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

  // Per-account month-end balance forecast. Only fetch for the current
  // selected period — past/future periods render a neutral "only
  // available for current period" state in the component, since the
  // stored balance is "now" and projecting it elsewhere would mislead.
  const loadAccountMonthEndForecast = useCallback(async () => {
    if (!realPeriodStart || !isCurrentSelectedPeriod) {
      accountForecastRequestId.current += 1;
      setAccountMonthEndForecast(null);
      setAccountMonthEndForecastError(false);
      return;
    }
    const myId = ++accountForecastRequestId.current;
    setAccountMonthEndForecastError(false);
    try {
      const data = await apiFetch<AccountMonthEndForecastResponse>(
        `/api/v1/forecast/account-balances?period_start=${realPeriodStart}`,
      );
      if (accountForecastRequestId.current !== myId) return;
      setAccountMonthEndForecast(data);
      setAccountMonthEndForecastError(false);
    } catch {
      if (accountForecastRequestId.current !== myId) return;
      setAccountMonthEndForecast(null);
      setAccountMonthEndForecastError(true);
    }
  }, [realPeriodStart, isCurrentSelectedPeriod]);

  useEffect(() => {
    if (!loading && user) {
      void loadAccountMonthEndForecast();
    }
  }, [loading, user, loadAccountMonthEndForecast]);

  // After a write from the AppShell-level "+ New Transaction" CTA, the
  // CTA dispatches `pfv:transaction-added` and we re-fetch the same
  // dashboard surfaces the old inline Quick Add form refreshed: refs
  // (account balances), period transactions, the projection (drives the
  // hero verdict), all-time pending, and per-account month-end balance.
  //
  // Promise.allSettled rather than fire-and-forget: a single failed
  // reload (network blip, backend hiccup) used to silently leave the
  // dashboard stale with no signal. Now we keep the optimistic UX
  // (interaction never blocks, prior snapshot stays on screen) and
  // surface a non-blocking inline banner with a Retry button when any
  // settled promise rejected. loadPendingTransactions and
  // loadAccountMonthEndForecast already swallow their own errors
  // internally, so we read their status from allSettled to detect any
  // backend hiccup uniformly.
  const refreshAllPostWrite = useCallback(async () => {
    if (loading || !user) return;
    setRefreshing(true);
    const results = await Promise.allSettled([
      loadRefs(),
      loadTransactions(0),
      loadForecastProjection(),
      loadPendingTransactions(),
      loadAccountMonthEndForecast(),
    ]);
    setRefreshing(false);
    setRefreshError(results.some((r) => r.status === "rejected"));
  }, [
    loading,
    user,
    loadRefs,
    loadTransactions,
    loadForecastProjection,
    loadPendingTransactions,
    loadAccountMonthEndForecast,
  ]);

  useTransactionAddedListener(() => {
    void refreshAllPostWrite();
  });

  const activeAccounts = accounts.filter((a) => a.is_active);
  // Empty-state copy for the recent-transactions list. Pre-PR this was
  // also used to gate the inline Quick Add button; the AppShell-level
  // CTA owns that now and gates itself, so this stays purely as the
  // "no rows yet" hint.
  const canAdd = activeAccounts.length > 0 && categories.length > 0;

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

  // Pending totals per account, computed from the all-time pending fetch
  // (NOT from the period-filtered allTransactions). Pending CC charges
  // must remain visible regardless of which billing period the user is
  // viewing — pending is a status, not a date.
  const pendingByAccount = pendingTransactions.reduce<Record<number, number>>((acc, tx) => {
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
  // donutData drives both the donut chart (always rendered in amount-desc
  // order so the largest slice starts at 12 o'clock) and the legend list
  // (sortable by name | percent | amount, persisted via spendingSort).
  const donutDataRaw = Object.entries(spendingByCategory)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value);
  const donutData = donutDataRaw;
  const totalSpend = donutDataRaw.reduce((s, d) => s + d.value, 0);
  const sortedSpending = (() => {
    const list = donutDataRaw.map((d) => ({
      name: d.name,
      value: d.value,
      pct: totalSpend > 0 ? (d.value / totalSpend) * 100 : 0,
      // Preserve original index so legend dots keep matching the donut's
      // color order regardless of how the rows are sorted.
      origIdx: donutDataRaw.indexOf(d),
    }));
    list.sort((a, b) => {
      let cmp = 0;
      if (spendingSort.field === "name") cmp = a.name.localeCompare(b.name);
      else if (spendingSort.field === "percent") cmp = a.pct - b.pct;
      else cmp = a.value - b.value;
      return spendingSort.dir === "asc" ? cmp : -cmp;
    });
    return list;
  })();

  // When chart filter is active, show from allTransactions; otherwise paginated
  const txSource = chartFilter ? allTransactions : transactions;

  // Dedup transfers
  const hiddenIds = new Set<number>();
  for (const tx of txSource) {
    if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) hiddenIds.add(tx.id);
  }
  const visibleTxs = txSource.filter((tx) => !hiddenIds.has(tx.id));


  function toggleDashSort(field: DashTxSort) {
    if (dashSortField === field) {
      dashTxSort.setSort(field, dashSortDir === "asc" ? "desc" : "asc");
    } else {
      dashTxSort.setSort(field, field === "date" ? "desc" : "asc");
    }
  }
  // Spending card: same toggle pattern. Numeric defaults flip to desc, name
  // defaults to asc (alphabetical) on first click.
  function toggleSpendingSort(field: SpendingSort) {
    if (spendingSort.field === field) {
      spendingSort.setSort(
        field,
        spendingSort.dir === "asc" ? "desc" : "asc",
      );
    } else {
      spendingSort.setSort(field, field === "name" ? "asc" : "desc");
    }
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
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-lg leading-none text-text-secondary hover:text-text-primary"
          >
            ×
          </button>
        </div>
      )}

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="dashboard-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAllPostWrite();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-5">
          {/* ═══ BILLING PERIOD — standalone nav bar ═══ */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button onClick={() => { setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1)); setChartFilter(null); }} disabled={periodIdx >= periods.length - 1} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Previous period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
              </button>
              <span className="text-sm font-medium text-text-primary">
                {monthFrom}{monthTo ? ` — ${monthTo}` : ""}
              </span>
              <button onClick={() => { setPeriodIdx(Math.max(periodIdx - 1, 0)); setChartFilter(null); }} disabled={periodIdx <= 0} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30" aria-label="Next period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
              </button>
              {selectedPeriod?.end_date === null && <span className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success">CURRENT</span>}
              {selectedPeriod?.end_date !== null && (
                <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) { setPeriodIdx(idx); setChartFilter(null); } }} className="ml-1 inline-flex min-h-[44px] items-center rounded-md px-3 text-xs font-medium text-text-muted hover:bg-surface-raised">Today</button>
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

          {/* ═══ ROW 2: Accounts sidebar + Forecast card, side-by-side ═══
              Tiles share ONE card with internal divider rows; the
              Forecast card on the right is the numeric authority for
              Balance + EOMF. Layout is three-tier: stacks vertically
              below `md`, equal 2-up columns from `md` to `lg`, then
              the 1fr/3fr split (forecast dominates) at `lg` and above.
              items-start so each card sits at its natural height
              (mismatch is intentional). */}
          {(() => {
            // Non-primary accounts sort alphabetically by name (locale-
            // aware, case-insensitive). Stable across transactions: a
            // coffee purchase can't reshuffle the sidebar the way a
            // balance-desc sort would.
            const defaultAcct = accountsWithBalance.find((a) => a.is_default);
            const others = accountsWithBalance
              .filter((a) => !a.is_default)
              .slice()
              .sort((a, b) =>
                a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
              );
            const orderedAccounts = defaultAcct
              ? [defaultAcct, ...others]
              : others;

            return (
              <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,3fr)]">
                <AccountTilesCard
                  accounts={orderedAccounts}
                  pendingByAccount={pendingByAccount}
                />
                <AccountMonthEndForecast
                  forecast={accountMonthEndForecast}
                  isCurrentPeriod={isCurrentSelectedPeriod}
                  hasAnyAccounts={activeAccounts.length > 0}
                  hasError={accountMonthEndForecastError}
                  onJumpToCurrent={() => {
                    const idx = periods.findIndex((p) => p.end_date === null);
                    if (idx >= 0) {
                      setPeriodIdx(idx);
                      setChartFilter(null);
                    }
                  }}
                />
              </div>
            );
          })()}

          {/* ═══ ROW 3: Three equal charts ═══ */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
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
                    <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
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
                  {/* D2 (2026-05-08): fill the description-to-amount
                      gap with a "% of total" column instead of leaving
                      it as dead whitespace. Layout: dot + name (flex-1
                      truncate) + percent (right-aligned, fixed col) +
                      amount (right-aligned, fixed col). Tabular-nums on
                      both numeric columns keeps digits aligned across
                      rows. */}
                  <div className="w-full space-y-1.5 sm:flex-1">
                    {/* Item 16 (D2): sortable column headers for Category,
                        %, Amount. Persists via usePersistedSort. The leading
                        "auto" column is the legend dot, which has no header.
                        Each header carries an aria-sort state and a lucide
                        chevron icon, with a brass focus ring matching the
                        Pressable-Surfaces Rule in DESIGN.md. */}
                    <div
                      role="row"
                      className="grid w-full grid-cols-[auto_minmax(0,1fr)_3rem_auto] items-center gap-2 px-1.5 pb-1 text-[10px] uppercase tracking-wider text-text-muted"
                    >
                      <span aria-hidden="true" className="h-2.5 w-2.5" />
                      <div
                        role="columnheader"
                        aria-sort={
                          spendingSort.field === "name"
                            ? spendingSort.dir === "asc"
                              ? "ascending"
                              : "descending"
                            : "none"
                        }
                      >
                        <button
                          type="button"
                          onClick={() => toggleSpendingSort("name")}
                          className="inline-flex items-center gap-1 text-left min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                          aria-label="Sort by category"
                        >
                          <span>Category</span>
                          {spendingSort.field === "name" ? (
                            spendingSort.dir === "asc" ? (
                              <ChevronUp className="h-3 w-3" aria-hidden="true" />
                            ) : (
                              <ChevronDown className="h-3 w-3" aria-hidden="true" />
                            )
                          ) : (
                            <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                          )}
                          <span className="sr-only">
                            {spendingSort.field === "name"
                              ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                              : "click to sort"}
                          </span>
                        </button>
                      </div>
                      <div
                        role="columnheader"
                        aria-sort={
                          spendingSort.field === "percent"
                            ? spendingSort.dir === "asc"
                              ? "ascending"
                              : "descending"
                            : "none"
                        }
                        className="text-right"
                      >
                        <button
                          type="button"
                          onClick={() => toggleSpendingSort("percent")}
                          className="inline-flex items-center gap-1 justify-end min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                          aria-label="Sort by percent of total"
                        >
                          <span>%</span>
                          {spendingSort.field === "percent" ? (
                            spendingSort.dir === "asc" ? (
                              <ChevronUp className="h-3 w-3" aria-hidden="true" />
                            ) : (
                              <ChevronDown className="h-3 w-3" aria-hidden="true" />
                            )
                          ) : (
                            <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                          )}
                          <span className="sr-only">
                            {spendingSort.field === "percent"
                              ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                              : "click to sort"}
                          </span>
                        </button>
                      </div>
                      <div
                        role="columnheader"
                        aria-sort={
                          spendingSort.field === "amount"
                            ? spendingSort.dir === "asc"
                              ? "ascending"
                              : "descending"
                            : "none"
                        }
                        className="text-right"
                      >
                        <button
                          type="button"
                          onClick={() => toggleSpendingSort("amount")}
                          className="inline-flex items-center gap-1 justify-end min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                          aria-label="Sort by amount"
                        >
                          <span>Amount</span>
                          {spendingSort.field === "amount" ? (
                            spendingSort.dir === "asc" ? (
                              <ChevronUp className="h-3 w-3" aria-hidden="true" />
                            ) : (
                              <ChevronDown className="h-3 w-3" aria-hidden="true" />
                            )
                          ) : (
                            <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                          )}
                          <span className="sr-only">
                            {spendingSort.field === "amount"
                              ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                              : "click to sort"}
                          </span>
                        </button>
                      </div>
                    </div>
                    {sortedSpending.slice(0, 10).map((d) => (
                      <button key={d.name} onClick={() => setChartFilter(chartFilter === d.name ? null : d.name)}
                        className={`grid w-full grid-cols-[auto_minmax(0,1fr)_3rem_auto] items-center gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-surface-raised ${chartFilter === d.name ? "bg-surface-overlay" : ""}`}>
                        <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CHART_COLORS[d.origIdx % CHART_COLORS.length] }} />
                        <span className="min-w-0 truncate text-left text-xs text-text-secondary">{d.name}</span>
                        <span className="text-right text-[10px] tabular-nums text-text-muted">{d.pct.toFixed(0)}%</span>
                        <span className="text-right text-xs tabular-nums text-text-muted">{formatAmount(d.value)}</span>
                      </button>
                    ))}
                    {sortedSpending.length > 10 && (
                      <p className="px-1.5 text-[10px] text-text-muted">+{sortedSpending.length - 10} more (click chart to filter)</p>
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
                <div className="w-full min-w-0 p-4" style={{ height: Math.max(budgets.slice(0, 6).length * 40, 100) }}>
                  <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                    <BarChart data={budgets.slice(0, 6).map((b) => ({
                      name: b.category_name,
                      spent: Number(b.spent),
                      remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
                      pct: b.percent_used,
                    }))} layout="vertical" margin={{ left: 0, right: 20, top: 0, bottom: 0 }}>
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="name" width={100} tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
                      <Tooltip
                        formatter={(v, name) => [
                          formatAmount(Number(v)),
                          name === "spent" ? <span style={{ color: chartColor.spent }}>Spent</span> : <span style={{ color: chartColor.remaining }}>Remaining</span>,
                        ]}
                        contentStyle={{ fontSize: "11px" }}
                      />
                      {/* D5 follow-up: shared BudgetSpentBarShape so
                          the spent bar rounds its right edge at >=100%
                          utilization (when the trailing remaining
                          segment collapses to zero). Static
                          radius={[4,0,0,4]} left those rows squared. */}
                      <Bar dataKey="spent" stackId="a" animationDuration={600}
                        cursor="pointer"
                        shape={(props: BudgetSpentBarShapeProps) => (
                          <BudgetSpentBarShape {...props} />
                        )}
                        onClick={(_, idx) => {
                          const name = budgets.slice(0, 6)[idx]?.category_name;
                          if (name) setChartFilter(chartFilter === name ? null : name);
                        }}
                      >
                        {budgets.slice(0, 6).map((b, i) => (
                          <Cell key={i} fill={b.percent_used > 100 ? chartColor.over : b.percent_used > 80 ? chartColor.watch : chartColor.spent} />
                        ))}
                      </Bar>
                      <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} animationDuration={600} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <div className="flex flex-wrap gap-3 px-4 pb-3 text-[10px] text-text-muted">
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.spent }} /> Spent</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.watch }} /> &gt;80%</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over budget</span>
                  <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.remaining }} /> Remaining</span>
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
                  const chartRows = expenseItems.slice(0, 8).map((it) => ({
                    name: it.category_name.length > 12 ? it.category_name.slice(0, 12) + "..." : it.category_name,
                    planned: Number(it.planned_amount),
                    actual: Number(it.actual_amount),
                  }));
                  return (
                    <div className="w-full min-w-0" style={{ height: Math.max(Math.min(expenseItems.length, 8) * 32, 100) }}>
                      <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                        <BarChart
                          data={chartRows}
                          layout="vertical"
                          margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
                        >
                          <XAxis type="number" hide />
                          <YAxis type="category" dataKey="name" width={90} tick={{ fill: chartColor.axisTick, fontSize: 10 }} />
                          <Tooltip
                            formatter={(v, name, item) => {
                              if (name === "planned") {
                                return [
                                  formatAmount(Number(v)),
                                  <span style={{ color: chartColor.planned }}>Planned</span>,
                                ];
                              }
                              const row = (item as { payload?: { planned: number; actual: number } } | undefined)?.payload;
                              const isOver = row ? row.actual > row.planned : false;
                              const labelColor = isOver ? chartColor.over : chartColor.actual;
                              return [
                                formatAmount(Number(v)),
                                <span style={{ color: labelColor }}>Actual</span>,
                              ];
                            }}
                            contentStyle={{ fontSize: "11px" }}
                          />
                          <Bar dataKey="planned" fill={chartColor.planned} radius={[4, 4, 4, 4]} animationDuration={600}
                            cursor="pointer"
                            onClick={(_, idx) => {
                              const name = expenseItems[idx]?.category_name;
                              if (name) setChartFilter(chartFilter === name ? null : name);
                            }}
                          />
                          <Bar dataKey="actual" fill={chartColor.actual} radius={[4, 4, 4, 4]} animationDuration={600}>
                            {chartRows.map((d, i) => (
                              <Cell
                                key={i}
                                fill={d.actual > d.planned ? chartColor.over : chartColor.actual}
                              />
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
              <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-text-muted">
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.planned }} /> Planned</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.actual }} /> Under plan</span>
                <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over plan</span>
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
                <button onClick={() => toggleDashSort("date")} className="w-16 text-left min-h-[32px] hover:text-text-primary">Date{dashSortField === "date" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
                <button onClick={() => toggleDashSort("description")} className="text-left min-h-[32px] hover:text-text-primary">Description{dashSortField === "description" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
              </div>
              <button onClick={() => toggleDashSort("amount")} className="min-h-[32px] hover:text-text-primary">Amount{dashSortField === "amount" ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}</button>
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
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={`text-sm font-medium tabular-nums ${isTransfer ? "text-info" : tx.type === "income" ? "text-success" : "text-danger"}`}>
                        {isTransfer ? "" : tx.type === "income" ? "+" : "-"}{formatAmount(tx.amount)}
                      </span>
                      {!isTransfer && (
                        <button
                          onClick={async () => {
                            try {
                              await apiFetch(`/api/v1/transactions/${tx.id}`, {
                                method: "PUT",
                                body: JSON.stringify({ status: tx.status === "settled" ? "pending" : "settled" }),
                              });
                              await loadTransactions(page);
                              await loadRefs();
                              void loadForecastProjection();
                              void loadAccountMonthEndForecast();
                              // Independent of `page` — a toggle on page 2
                              // still has to refresh the strip's totals.
                              void loadPendingTransactions();
                            } catch (err) {
                              setError(extractErrorMessage(err));
                            }
                          }}
                          aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                          aria-pressed={tx.status === "settled"}
                          className="inline-flex min-h-[44px] items-center"
                        >
                          {/* Outer button carries the WCAG 2.5.8
                              touch-target hit area; inner span keeps
                              the lean visual that matches /transactions.
                              Pending uses the warning token so it reads
                              as actionable, not muted gray. */}
                          <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-warning-dim text-warning"}`}>
                            {tx.status}
                          </span>
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
                <button onClick={() => setPage(Math.max(0, page - 1))} disabled={page === 0} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-border px-3 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40">Prev</button>
                <span className="text-xs text-text-muted">Page {page + 1}</span>
                <button onClick={() => setPage(page + 1)} disabled={!hasMore} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-border px-3 text-xs text-text-secondary hover:bg-surface-raised disabled:opacity-40">Next</button>
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
