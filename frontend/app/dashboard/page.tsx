"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import HelpTooltip from "@/components/Tooltip";
import Pagination from "@/components/ui/Pagination";
import TourAnchor from "@/components/tour/TourAnchor";
import { useAuth } from "@/components/auth/AuthProvider";
import CustomDashboard from "@/components/dashboard/CustomDashboard";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { fetchAll } from "@/lib/pagination";
import { formatAmount, formatLocalDate, projectedPeriodEnd, todayISO } from "@/lib/format";
import { btnSecondary, card, cardHeader, cardTitle, pageTitle, error as errorCls } from "@/lib/styles";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";


import { PieChart, Pie, BarChart, Bar, XAxis, YAxis, Cell, Tooltip, ResponsiveContainer } from "recharts";
import { chartColor, CHART_SERIES } from "@/lib/chart-colors";
import { SeriesTooltip } from "@/components/charts/SeriesTooltip";
import {
  resolveBudgetSeries,
  resolveForecastSeries,
} from "@/lib/reports/chart-series-tooltip";
import { BudgetSpentBarShape, type BudgetSpentBarShapeProps } from "@/lib/chart-shapes";
import OnTrackTile from "@/components/dashboard/OnTrackTile";
import AIForecastRefineToggle from "@/components/dashboard/AIForecastRefineToggle";
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

function transactionHighlightHref(tx: Transaction) {
  // The transactions list filters by `effective_period_date_expr =
  // COALESCE(settled_date, date)`, so a deep link built from `tx.date`
  // misses any row whose settled_date differs from its purchase date —
  // notably every credit-card transaction settling on a later statement
  // close. Use the same coalesce here so the row we want highlighted
  // actually lands inside the queried window.
  const effectiveDate = tx.settled_date ?? tx.date;
  const params = new URLSearchParams({
    account_id: String(tx.account_id),
    transaction_id: String(tx.id),
    date_from: effectiveDate,
    date_to: effectiveDate,
  });

  return `/transactions?${params.toString()}`;
}

// Layout-shaped loading state. Product guidance prefers a skeleton that
// mirrors the real structure (period bar → hero → accounts/forecast →
// three charts → transactions) over a centered spinner: it cuts perceived
// latency and avoids the layout shift when data lands. Purely decorative —
// aria-hidden, with a single sr-only status line for assistive tech. The
// global prefers-reduced-motion block neutralizes the pulse automatically.
function DashboardSkeleton() {
  const block = "rounded-md bg-surface-raised";
  return (
    <div role="status" aria-busy="true" className="space-y-5">
      <span className="sr-only">Loading dashboard…</span>
      <div aria-hidden="true" className="flex items-center justify-between">
        <div className={`h-8 w-48 ${block}`} />
        <div className={`h-4 w-32 ${block}`} />
      </div>
      <div aria-hidden="true" className={`${card} h-32 animate-pulse`} />
      <div aria-hidden="true" className="grid grid-cols-1 items-start gap-4 md:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,3fr)]">
        <div className={`${card} h-40 animate-pulse`} />
        <div className={`${card} h-40 animate-pulse`} />
      </div>
      <div aria-hidden="true" className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        <div className={`${card} h-48 animate-pulse`} />
        <div className={`${card} h-48 animate-pulse`} />
        <div className={`${card} h-48 animate-pulse`} />
      </div>
      <div aria-hidden="true" className={`${card} h-64 animate-pulse`} />
    </div>
  );
}

/**
 * Flag-switched entry point for /dashboard.
 *
 * When ``features.customDashboard`` is **false** (default): renders the
 * existing ``LegacyDashboard`` component, which is the original page body
 * extracted verbatim — no behaviour change, byte-identical render.
 *
 * When **true**: renders ``<CustomDashboard />``, the Canvas-based shell
 * introduced in W4 Phase 1.
 *
 * While auth is still loading we render the legacy component (it shows
 * its own skeleton via the ``loading`` flag it already reads from
 * ``useAuth()``), so the UX during the loading window is unchanged.
 */
export default function DashboardPage() {
  const { features } = useAuth();
  if (features?.customDashboard) {
    return <CustomDashboard />;
  }
  return <LegacyDashboard />;
}

function LegacyDashboard() {
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
  const [txTotal, setTxTotal] = useState(0);
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
  type DashTxSort = "date" | "description" | "status" | "amount";
  const dashTxSort = usePersistedSort<DashTxSort>(
    SORT_KEY_DASHBOARD_TRANSACTIONS,
    "date",
    "desc",
    ["date", "description", "status", "amount"] as const,
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
      apiFetch<{ items: Transaction[]; total: number }>(`/api/v1/transactions?limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}&${dateFilter}`),
      p === 0 ? apiFetch<{ items: Transaction[]; total: number }>(`/api/v1/transactions?limit=200&${dateFilter}`) : null,
      p === 0 ? apiFetch<Budget[]>(budgetUrl) : null,
      p === 0 ? apiFetch<ForecastPlan | null>(forecastUrl) : null,
    ]);
    const page_txs = pageData?.items ?? [];
    setTxTotal(pageData?.total ?? 0);
    setTransactions(page_txs);
    if (allData) setAllTransactions(allData.items);
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

  // All active accounts for individual tiles
  const accountsWithBalance = activeAccounts;

  // Precompute tx map for O(1) linked lookups. Memoized so it isn't
  // rebuilt on every unrelated render (hover, sort toggle, period nav).
  const txMap = useMemo(
    () => new Map(allTransactions.map((tx) => [tx.id, tx])),
    [allTransactions],
  );

  // Pending totals per account, computed from the all-time pending fetch
  // (NOT from the period-filtered allTransactions). Pending CC charges
  // must remain visible regardless of which billing period the user is
  // viewing — pending is a status, not a date.
  const pendingByAccount = useMemo(
    () =>
      pendingTransactions.reduce<Record<number, number>>((acc, tx) => {
        const sign = tx.type === "income" ? 1 : -1;
        acc[tx.account_id] = (acc[tx.account_id] || 0) + Number(tx.amount) * sign;
        return acc;
      }, {}),
    [pendingTransactions],
  );

  // Spending by category from all period transactions. Transfer expense
  // halves carry linked_transaction_id; excluding them here stops transfers
  // from polluting the Spending by Category donut.
  //
  // donutData drives both the donut chart (always rendered in amount-desc
  // order so the largest slice starts at 12 o'clock) and the legend list
  // (sortable by name | percent | amount, persisted via spendingSort).
  //
  // Memoized so unrelated parent renders (period nav, filter toggles,
  // edit-mode flips) don't rebuild the array reference and force Recharts
  // to re-layout the donut on every render.
  const donutDataRaw = useMemo(() => {
    if (!Array.isArray(allTransactions)) return [];
    const spendingByCategory = allTransactions
      .filter(
        (tx) =>
          tx.type === "expense" &&
          tx.status === "settled" &&
          tx.linked_transaction_id == null,
      )
      .reduce<Record<string, number>>((acc, tx) => {
        acc[tx.category_name] = (acc[tx.category_name] || 0) + Number(tx.amount);
        return acc;
      }, {});
    return Object.entries(spendingByCategory)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [allTransactions]);
  const donutData = donutDataRaw;
  const totalSpend = useMemo(
    () => donutDataRaw.reduce((s, d) => s + d.value, 0),
    [donutDataRaw],
  );
  const sortedSpending = useMemo(() => {
    const list = donutDataRaw.map((d, i) => ({
      name: d.name,
      value: d.value,
      pct: totalSpend > 0 ? (d.value / totalSpend) * 100 : 0,
      // Preserve original index so legend dots keep matching the donut's
      // color order regardless of how the rows are sorted. Uses the map
      // index directly (was donutDataRaw.indexOf(d), an O(n^2) scan).
      origIdx: i,
    }));
    list.sort((a, b) => {
      let cmp = 0;
      if (spendingSort.field === "name") cmp = a.name.localeCompare(b.name);
      else if (spendingSort.field === "percent") cmp = a.pct - b.pct;
      else cmp = a.value - b.value;
      return spendingSort.dir === "asc" ? cmp : -cmp;
    });
    return list;
  }, [donutDataRaw, totalSpend, spendingSort.field, spendingSort.dir]);

  // When chart filter is active, show from allTransactions; otherwise
  // paginated. Dedups transfer legs (keep the lower-id half). Memoized so
  // the Set build + filter don't rerun on every unrelated render.
  const visibleTxs = useMemo(() => {
    const txSource = chartFilter ? allTransactions : transactions;
    const hiddenIds = new Set<number>();
    for (const tx of txSource) {
      if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) hiddenIds.add(tx.id);
    }
    return txSource.filter((tx) => !hiddenIds.has(tx.id));
  }, [chartFilter, allTransactions, transactions]);

  // First six budgets feed the "Budget Overview" mini bar chart on the
  // dashboard. Memoizing prevents Recharts from re-laying out the bars
  // every time an unrelated piece of dashboard state (sort toggle,
  // expansion, hover) re-renders the parent.
  //
  // Defensive Array.isArray guard: some API responses return objects on
  // empty/error paths, and the chart is only rendered when budgets is
  // actually populated — we just don't want this hoisted slice to throw
  // before that conditional renders.
  const dashBudgets = useMemo(
    () => (Array.isArray(budgets) ? budgets.slice(0, 6) : []),
    [budgets],
  );
  const budgetChartData = useMemo(
    () =>
      dashBudgets.map((b) => ({
        name: b.category_name,
        spent: Number(b.spent),
        remaining: Math.max(Number(b.amount) - Number(b.spent), 0),
        pct: b.percent_used,
      })),
    [dashBudgets],
  );

  // First eight expense items feed the "Forecast by Category" mini bar
  // chart. Same memoization rationale as the donut and budget charts.
  const forecastExpenseItems = useMemo(
    () => forecast?.items.filter((it) => it.type === "expense") ?? [],
    [forecast],
  );
  const forecastChartRows = useMemo(
    () =>
      forecastExpenseItems.slice(0, 8).map((it) => ({
        categoryId: it.category_id,
        name:
          it.category_name.length > 12
            ? it.category_name.slice(0, 12) + "..."
            : it.category_name,
        planned: Number(it.planned_amount),
        actual: Number(it.actual_amount),
      })),
    [forecastExpenseItems],
  );


  function toggleDashSort(field: DashTxSort) {
    if (dashSortField === field) {
      dashTxSort.setSort(field, dashSortDir === "asc" ? "desc" : "asc");
    } else {
      // Default direction per field: date desc (newest first), description /
      // status asc (alphabetical: pending before settled), amount asc.
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

  // Sort + filter the visible transactions. Memoized on its real inputs so
  // a hover or AI-toggle flip doesn't re-sort the whole list.
  const sortedVisibleTxs = useMemo(
    () =>
      visibleTxs
        .filter((tx) => !chartFilter || tx.category_name === chartFilter)
        .sort((a, b) => {
          let cmp = 0;
          if (dashSortField === "date") cmp = a.date.localeCompare(b.date);
          else if (dashSortField === "description") cmp = a.description.localeCompare(b.description);
          // Status sort is alphabetical on the enum value: "pending" < "settled"
          // so asc surfaces pending rows first (what the user wants to act on),
          // desc surfaces settled first.
          else if (dashSortField === "status") cmp = a.status.localeCompare(b.status);
          else if (dashSortField === "amount") cmp = Number(a.amount) - Number(b.amount);
          return dashSortDir === "asc" ? cmp : -cmp;
        }),
    [visibleTxs, chartFilter, dashSortField, dashSortDir],
  );


  return (
    <AppShell>
      <TourAnchor id="dashboard.header" as="child">
        <div className="mb-6 flex items-center justify-between">
          <div className="flex items-start gap-1">
            <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
            <HelpAnchor section="dashboard" label="Dashboard" />
          </div>
          <div className="flex items-center gap-2">
            <TourAnchor id="dashboard.import-cta" as="child">
              <Link href="/import" className={btnSecondary}>
                Import
              </Link>
            </TourAnchor>
          </div>
        </div>
      </TourAnchor>

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
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-lg leading-none text-text-secondary hover:text-text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
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
        <DashboardSkeleton />
      ) : (
        <div className="space-y-5">
          {/* ═══ BILLING PERIOD — standalone nav bar ═══ */}
          <TourAnchor id="dashboard.period-nav" as="child">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <button onClick={() => { setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1)); setChartFilter(null); }} disabled={periodIdx >= periods.length - 1} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30" aria-label="Previous period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
              </button>
              <span className="text-sm font-medium text-text-primary">
                {monthFrom}{monthTo ? ` – ${monthTo}` : ""}
              </span>
              <button onClick={() => { setPeriodIdx(Math.max(periodIdx - 1, 0)); setChartFilter(null); }} disabled={periodIdx <= 0} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30" aria-label="Next period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
              </button>
              {selectedPeriod?.end_date === null && <span className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success">CURRENT</span>}
              {selectedPeriod?.end_date !== null && (
                <button onClick={() => { const idx = periods.findIndex((p) => p.end_date === null); if (idx >= 0) { setPeriodIdx(idx); setChartFilter(null); } }} className="ml-1 inline-flex min-h-[44px] items-center rounded-md px-3 text-xs font-medium text-text-muted hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">Today</button>
              )}
            </div>
            <Link href="/transactions" className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary">View All Transactions</Link>
          </div>
          </TourAnchor>

          {/* ═══ ROW 1: On Track hero — single full-width tile ═══
              `as="child"` is REQUIRED. The child is a block-level
              `<div className="flex items-start gap-2">` that sits as a
              direct child of `<div className="space-y-5">`. Without
              `as="child"`, TourAnchor wraps it in a bare inline
              `<span>`. Tailwind's `space-y-5` rule applies
              `margin-block-end: 1.25rem` to every direct child except
              the last, but vertical margins on inline elements are
              ignored by the CSS box model. The gap between the period
              nav, this hero, and the accounts/forecast grid below
              therefore collapses (PR #226 regression visible on prod).
              Every other TourAnchor on this page already uses
              `as="child"`; this one was the lone exception. */}
          {/* On Track row: the `?` lives INSIDE the tile's top-right
              corner via a positioned overlay, mirroring the
              AccountMonthEndForecast tile below. The previous layout
              put HelpTooltip in a sibling flex column so it visibly
              protruded outside the card border (owner bug report
              2026-05-13). */}
          <TourAnchor id="dashboard.on-track-tile" as="child">
            <div className="relative">
              <OnTrackTile
                forecastPlan={forecast}
                projection={forecastProjection}
                projectionFailed={projectionFailed}
                projectionLoading={projectionLoading}
                onRetryProjection={() => void loadForecastProjection()}
                isPastPeriod={isPastSelectedPeriod}
                isFuturePeriod={isFutureSelectedPeriod}
              />
              <div className="absolute right-3 top-3">
                <HelpTooltip
                  content="On Track compares spent so far to your planned spending for this period. Watch warns at 95 percent, Over at 105 percent."
                  learnMoreSection="forecasts"
                  triggerLabel="What does On Track mean?"
                />
              </div>
              {/* LAI.2: opt-in AI refinement. Hides itself on 403
                  (feature gate closed) so users without the AI tier
                  never see the toggle. Only surfaces in the current
                  period — past/future-period forecasts are
                  deterministic + locked, refinement would mislead. */}
              {!isPastSelectedPeriod && !isFutureSelectedPeriod && (
                <AIForecastRefineToggle periodStart={realPeriodStart} />
              )}
            </div>
          </TourAnchor>

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
                <TourAnchor id="dashboard.account-forecast" as="child">
                  <div className="relative">
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
                    <div className="absolute right-3 top-3">
                      <HelpTooltip
                        content="Each account's current balance plus its pending transactions in this billing period. Recurring activity is not factored in."
                        learnMoreSection="forecasts"
                        triggerLabel="How is the end of month forecast calculated?"
                      />
                    </div>
                  </div>
                </TourAnchor>
              </div>
            );
          })()}

          {/* ═══ ROW 3: Three equal charts ═══ */}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {/* Spending by category (donut) */}
            <div className={`${card} p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Spending by Category</h2>
              {chartFilter && (
                <button onClick={() => setChartFilter(null)} className="mb-2 rounded-md bg-surface-overlay px-2.5 py-1 text-xs text-text-secondary hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">
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
                            <Cell key={d.name} fill={CHART_SERIES[i % CHART_SERIES.length]}
                              opacity={chartFilter && chartFilter !== d.name ? 0.3 : 1} />
                          ))}
                        </Pie>
                        {/* Single-series pie: recharts renders the slice
                            name itself, so a value `formatter` is enough.
                            SeriesTooltip is only needed for the multi-series
                            bar charts where the name node failed to render. */}
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
                        <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CHART_SERIES[d.origIdx % CHART_SERIES.length] }} />
                        <span className="min-w-0 truncate text-left text-xs text-text-secondary">{d.name}</span>
                        {/* %/amount carry data, so they ride text-secondary
                            (~6.9:1) not text-muted (~3.0:1, fails AA 1.4.3). */}
                        <span className="text-right text-[10px] tabular-nums text-text-secondary">{d.pct.toFixed(0)}%</span>
                        <span className="text-right text-xs tabular-nums text-text-secondary">{formatAmount(d.value)}</span>
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
                <div className="w-full min-w-0 p-4" style={{ height: Math.max(dashBudgets.length * 40, 100) }}>
                  <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                    <BarChart data={budgetChartData} layout="vertical" margin={{ left: 0, right: 20, top: 0, bottom: 0 }}>
                      <XAxis type="number" hide />
                      <YAxis type="category" dataKey="name" width={100} tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
                      <Tooltip
                        content={
                          <SeriesTooltip format={formatAmount} resolve={resolveBudgetSeries} />
                        }
                      />
                      {/* D5 follow-up: shared BudgetSpentBarShape so
                          the spent bar rounds its right edge at >=100%
                          utilization (when the trailing remaining
                          segment collapses to zero). Static
                          radius={[4,0,0,4]} left those rows squared. */}
                      <Bar dataKey="spent" stackId="a" animationDuration={220}
                        cursor="pointer"
                        shape={(props: BudgetSpentBarShapeProps) => (
                          <BudgetSpentBarShape {...props} />
                        )}
                        onClick={(_, idx) => {
                          const name = dashBudgets[idx]?.category_name;
                          if (name) setChartFilter(chartFilter === name ? null : name);
                        }}
                      >
                        {dashBudgets.map((b) => (
                          <Cell key={b.category_id} fill={b.percent_used > 100 ? chartColor.over : b.percent_used > 80 ? chartColor.watch : chartColor.spent} />
                        ))}
                      </Bar>
                      <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} animationDuration={220} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
                <div className="flex flex-wrap gap-3 px-4 pb-3 text-[10px] text-text-secondary">
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
                if (forecast && forecastExpenseItems.length > 0) {
                  return (
                    <div className="w-full min-w-0" style={{ height: Math.max(Math.min(forecastExpenseItems.length, 8) * 32, 100) }}>
                      <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                        <BarChart
                          data={forecastChartRows}
                          layout="vertical"
                          margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
                        >
                          <XAxis type="number" hide />
                          <YAxis type="category" dataKey="name" width={90} tick={{ fill: chartColor.axisTick, fontSize: 10 }} />
                          <Tooltip
                            content={
                              <SeriesTooltip format={formatAmount} resolve={resolveForecastSeries} />
                            }
                          />
                          <Bar dataKey="planned" fill={chartColor.planned} radius={[4, 4, 4, 4]} animationDuration={220}
                            cursor="pointer"
                            onClick={(_, idx) => {
                              const name = forecastExpenseItems[idx]?.category_name;
                              if (name) setChartFilter(chartFilter === name ? null : name);
                            }}
                          />
                          <Bar dataKey="actual" fill={chartColor.actual} radius={[4, 4, 4, 4]} animationDuration={220}>
                            {forecastChartRows.map((d) => (
                              <Cell
                                key={d.categoryId}
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
              <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-text-secondary">
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
            {/* Sortable mini-header. Column order mirrors /transactions:
                Date / Description / Status / Amount. Hidden under sm; mobile
                rows collapse to a two-line layout (see below) where header
                labels are redundant. */}
            <div className="hidden sm:block border-b border-border-subtle px-5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">
              <div className="grid grid-cols-12 items-center gap-3">
                {([
                  { field: "date" as const, label: "Date", span: "col-span-2", align: "text-left" },
                  { field: "description" as const, label: "Description", span: "col-span-6", align: "text-left" },
                  { field: "status" as const, label: "Status", span: "col-span-2", align: "text-center" },
                  { field: "amount" as const, label: "Amount", span: "col-span-2", align: "text-right" },
                ]).map((col) => {
                  const active = dashSortField === col.field;
                  // min-h-[32px] is a deliberate dense-header exception: it
                  // clears WCAG 2.5.8 (24px AA floor) without inflating the
                  // table header to the 44px primary-control floor. Locked by
                  // dashboard-sort-header-touch-targets.test.tsx. The visible
                  // ↑/↓ arrow stays in textContent for sighted users and the
                  // columns test; aria-label carries the same state to AT.
                  return (
                    <button
                      key={col.field}
                      onClick={() => toggleDashSort(col.field)}
                      // "Sort transactions by …" is deliberately distinct from
                      // the Spending card's "Sort by …" labels so role-name
                      // queries stay unambiguous across the two sortable tables.
                      aria-label={
                        active
                          ? `Transactions sorted by ${col.label.toLowerCase()}, ${dashSortDir === "asc" ? "ascending" : "descending"}. Activate to reverse.`
                          : `Sort transactions by ${col.label.toLowerCase()}`
                      }
                      className={`${col.span} ${col.align} min-h-[32px] rounded-sm hover:text-text-primary transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30`}
                    >
                      {col.label}{active ? (dashSortDir === "asc" ? " ↑" : " ↓") : ""}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="divide-y divide-border-subtle">
              {sortedVisibleTxs.map((tx) => {
                const isTransfer = tx.linked_transaction_id !== null;
                const linkedTx = isTransfer ? txMap.get(tx.linked_transaction_id!) : null;
                const amountClass = `text-sm font-medium tabular-nums ${isTransfer ? "text-info" : tx.type === "income" ? "text-success" : "text-danger"}`;
                const amountText = `${isTransfer ? "" : tx.type === "income" ? "+" : "-"}${formatAmount(tx.amount)}`;
                const subline = isTransfer && linkedTx ? (
                  <>{tx.account_name} &rarr; {linkedTx.account_name}</>
                ) : (
                  <>{tx.account_name} &middot; {tx.category_name}</>
                );
                const statusPill = !isTransfer ? (
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
                        // Independent of `page`: a toggle on page 2
                        // still has to refresh the strip's totals.
                        void loadPendingTransactions();
                      } catch (err) {
                        setError(extractErrorMessage(err));
                      }
                    }}
                    aria-label={`Mark as ${tx.status === "settled" ? "pending" : "settled"}`}
                    aria-pressed={tx.status === "settled"}
                    className="inline-flex min-h-[44px] items-center rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  >
                    {/* Outer button carries the WCAG 2.5.8 touch target;
                        inner span matches /transactions' pill visual. */}
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${tx.status === "settled" ? "bg-success-dim text-success" : "bg-warning-dim text-warning"}`}>
                      {tx.status}
                    </span>
                  </button>
                ) : null;
                return (
                  <div key={tx.id} className="px-5 py-2.5">
                    {/* Responsive single-tree row. On sm+, this is a 12-col
                        grid mirroring the header (Date / Description / Status
                        / Amount). Below sm, the wrapper drops to a flex
                        column so we get a two-line layout: line 1 the link
                        (date + description + subline), line 2 the status
                        pill + amount on the right. Single Link/pill node so
                        deep-link tests that match `findByRole("link", ...)`
                        keep working. */}
                    <div className="flex flex-col gap-1.5 sm:grid sm:grid-cols-12 sm:items-center sm:gap-3">
                      <Link
                        href={transactionHighlightHref(tx)}
                        className="-mx-2 -my-1.5 flex min-w-0 items-center gap-3 rounded-md px-2 py-1.5 transition-colors hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent sm:col-span-8 sm:my-0"
                      >
                        {/* Date + Settled date. The operator requires the
                            settled date visible wherever a transaction renders,
                            so each row stacks the original date over the settled
                            date (settled date when set, em-dash when still
                            pending / unsettled). MM-DD slice matches the
                            compact recent-list date format. */}
                        <span className="flex w-16 shrink-0 flex-col text-xs tabular-nums text-text-secondary sm:w-auto">
                          <span>{tx.date.slice(5)}</span>
                          <span className="text-[10px] text-text-muted" data-testid={`dash-settled-${tx.id}`}>
                            {tx.settled_date ? tx.settled_date.slice(5) : "—"}
                          </span>
                        </span>
                        <div className="min-w-0">
                          <p className="text-sm text-text-primary truncate">{tx.description}</p>
                          <p className="text-[11px] text-text-secondary truncate">{subline}</p>
                        </div>
                      </Link>
                      {/* Status + Amount: on desktop these split into their
                          own columns (col-span-2 each). On mobile they share
                          one flex row indented under the description. */}
                      <div className="flex items-center justify-between gap-2 pl-[4.75rem] sm:contents sm:pl-0">
                        <div className="sm:col-span-2 sm:flex sm:justify-center">
                          {statusPill}
                        </div>
                        <div className="sm:col-span-2 sm:text-right">
                          <span className={amountClass}>{amountText}</span>
                        </div>
                      </div>
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
            {!chartFilter && (txTotal > PAGE_SIZE || page > 0) && (
              <div className="border-t border-border px-5">
                <Pagination
                  page={page + 1}
                  pageSize={PAGE_SIZE}
                  total={txTotal}
                  onPageChange={(n) => setPage(n - 1)}
                  onPageSizeChange={() => {}}
                  showPageSizeSelector={false}
                />
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
