"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronDown, ChevronUp, ChevronsUpDown, RefreshCw } from "lucide-react";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import HelpTooltip from "@/components/Tooltip";
import Pagination from "@/components/ui/Pagination";
import TourAnchor from "@/components/tour/TourAnchor";
import { useAuth } from "@/components/auth/AuthProvider";
import CustomDashboard from "@/components/dashboard/CustomDashboard";
import type { SpendingByCategoryResponse } from "@/components/dashboard/DashboardDataProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { fetchAll } from "@/lib/pagination";
import { formatAmount, formatLocalDate, projectedPeriodEnd, todayISO } from "@/lib/format";
import { periodStatus, selectCurrentPeriodIndex } from "@/lib/billingPeriodStatus";
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
//
// TBD-221: `categories` here is NOT the donut's source and must not become it.
// This route is gated by TBD-197, and the donut is a historical-actuals tile;
// it reads GET /api/v1/transactions/spending-by-category instead. `unknown[]`
// keeps that door shut.
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
  const { user, loading, features } = useAuth();
  // TBD-197. `=== false`, never truthiness: undefined means a booting client
  // (or a pre-existing test mock) and Budgets ships ON.
  const budgetsDisabled = features?.budgets === false;
  // TBD-197 PR 2. Same `=== false` rule. Note what it does NOT cover:
  // `loadAccountMonthEndForecast` and the AccountMonthEndForecast tile stay
  // live, because `/forecast/account-balances` is an account-projection engine
  // (credit-card cycles + loan amortization) that is ungated server-side.
  const forecastDisabled = features?.forecast === false;
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- client-only mount read of the ?reset=1 URL param to show the reset banner
      setResetBanner(true);
      router.replace("/dashboard");
    }
  }, [router]);

  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  // ⚠ TBD-221 deleted the sibling `allTransactions` (limit=200) snapshot. Its
  // two consumers — the donut memo and the chart-filter branch of `visibleTxs`
  // — are both replaced by server work: the rollup and a paginated drilldown.
  // That is how the 200-row cap dies, by removing the only fetch that had one.
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
  // TBD-221 — the Spending donut's source, and its OWN failure/loading flags.
  // `/api/v1/transactions/spending-by-category` is ungated; `/api/v1/forecast`
  // is not. One flag across both would blank a working donut whenever the
  // forecast failed (or was simply switched off), and blank a working forecast
  // whenever the rollup failed.
  //
  // ⚠ STORED WITH THE PERIOD IT WAS REQUESTED FOR, so the last good payload can
  // survive a refetch (see `activeRollup`) without ever rendering one period's
  // numbers under another period's heading.
  const [spendingRollup, setSpendingRollup] = useState<{
    periodStart: string;
    data: SpendingByCategoryResponse;
  } | null>(null);
  const [rollupFailed, setRollupFailed] = useState(false);
  // Raw in-flight flag; the tile reads the wider `rollupLoading` derived below.
  const [rollupFetching, setRollupFetching] = useState(false);
  const rollupRequestId = useRef(0);
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

  // TBD-221: a category_id (the rollup's identity), not a category name.
  const [chartFilterId, setChartFilterId] = useState<number | null>(null);
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
  // TBD-242: one classifier, one LOCAL clock (`todayISO`), never UTC.
  const _today = todayISO();
  const _selectedStatus = selectedPeriod ? periodStatus(selectedPeriod, _today) : null;
  const isCurrentSelectedPeriod = _selectedStatus === "open";
  const isPastSelectedPeriod = _selectedStatus === "past";
  const isFutureSelectedPeriod = _selectedStatus === "upcoming";
  // monthFrom drives transaction date filters (which don't go through
  // resolve_period), so the calendar fallback is fine there.
  const monthFrom = realPeriodStart ?? formatLocalDate(new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  // For open periods, compute expected end from billing cycle day.
  //
  // ⚠ DISPLAY WINDOW, NEVER AN ANALYSIS BOUND (TBD-221 / TBD-243). The open-
  // period arm is a client calendar formula; every total on this screen is
  // bounded server-side by `period_spend_window_end`. `monthTo` may only scope
  // the UNFILTERED Recent Transactions list, which is a ledger view and sums to
  // nothing. Anything that produces or drills into a number uses `rollupTo`.
  const monthTo =
    selectedPeriod?.end_date
    ?? (monthFrom ? projectedPeriodEnd(monthFrom, billingCycleDay) ?? "" : "");

  // ── The rollup's own analysis window (TBD-221) ─────────────────────────────
  // Off the SAME payload as the per-category totals, so window and numbers
  // arrive together and cannot drift.
  //
  // ⚠ READ BACK OFF THE RESPONSE, never `realPeriodStart`: `period_start` on
  // the request is a hint the server may silently substitute with the current
  // period (no 404, no 422), so a window built from the value we SENT can
  // describe a different period from the numbers we got.
  //
  // ⚠ THE LAST GOOD ROLLUP SURVIVES A REFETCH (PR 630 review, B1/B2). It is
  // dropped for exactly one reason — the selected period no longer matches the
  // one it was fetched for — and NOT because a request happens to be open.
  // Nulling it before every fetch made the window, and therefore the drilldown
  // below, a function of whether a network response was currently in hand.
  // A failed refetch keeps it too: the fetch failed, the WINDOW did not change.
  const activeRollup =
    spendingRollup !== null && spendingRollup.periodStart === realPeriodStart
      ? spendingRollup.data
      : null;
  const rollupFrom = activeRollup?.period_start ?? null;
  const rollupTo = activeRollup?.period_end ?? null;

  // "No numbers for the selected period yet". The second arm covers the render
  // between a period change (which invalidates `activeRollup` immediately) and
  // the effect that starts the next fetch — without it the tile flashes "No
  // expense data yet" for a frame on every period nav.
  const rollupLoading =
    rollupFetching
    || (realPeriodStart !== null && activeRollup === null && !rollupFailed);

  // A drilldown is only meaningful while that window is known: the filtered
  // query has to reproduce the slice's WHERE clause and the window is half of
  // it. With no rollup for this period the filter reads null and the tile falls
  // back to the plain period page, rather than guessing a bound from `monthTo`.
  const chartFilter = rollupFrom && rollupTo ? chartFilterId : null;
  const setChartFilter = useCallback((c: number | null) => {
    setChartFilterId(c);
    // The drilldown is its own paginated result set.
    setPage(0);
  }, []);

  const loadRefs = useCallback(async () => {
    // TBD-197: the budgets call sits INSIDE this Promise.all, and a rejection
    // here does not degrade one tile — it replaces the entire page with
    // "Failed to load dashboard data" (see the catch on the effect below).
    // With Budgets gated off the route 404s, so the call must not be made at
    // all. Fenced by G6.
    const [accts, cats, bds, per, plist, bc] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<Category[]>("/api/v1/categories"),
      budgetsDisabled ? null : apiFetch<Budget[]>("/api/v1/budgets"),
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
    // Default to the current period (TBD-242), not index 0.
    const currentIdx = selectCurrentPeriodIndex(pl);
    if (currentIdx >= 0) setPeriodIdx(currentIdx);
  }, [budgetsDisabled]);

  // ── The recent-tx list query, in its two shapes (TBD-221) ──────────────────
  //
  // Plain string, deliberately not a memo: `useCallback` compares deps with
  // Object.is and two equal strings are Object.is-equal, so `loadTransactions`
  // keeps a stable identity for as long as the URL it would build is unchanged
  // — which is what stops the projection landing (rollup window null → known)
  // re-firing an identical unfiltered page fetch.
  //
  //  • UNFILTERED — the "Recent Transactions" ledger view over the DISPLAY
  //    window. It is a list, not a total; `monthTo` may bound it.
  //
  //  • FILTERED — the drilldown into one rollup slice, reproducing that
  //    slice's WHERE clause exactly so the list cannot show a row the slice
  //    excluded, nor sum past the number the user clicked.
  //
  //    ⚠ `category_match=exact` is NOT optional. `category_id` on the list
  //    endpoint is master-includes-subs (a 2026-05-13 regression guard) while
  //    the rollup groups by the row's OWN category_id. Without it a master
  //    slice opens a list summing to more than itself.
  //
  //    ⚠ `collapse_transfers` is deliberately ABSENT: `reportable=true`
  //    already excludes every non-null `linked_transaction_id`, a strict
  //    superset of it.
  const txQueryTail =
    chartFilter !== null && rollupFrom && rollupTo
      ? `&category_id=${chartFilter}&category_match=exact&reportable=true`
        + `&type=expense&status=settled`
        + `&date_from=${rollupFrom}&date_to=${rollupTo}`
      : `&collapse_transfers=true&date_from=${monthFrom}`
        + `${monthTo ? `&date_to=${monthTo}` : ""}`;

  const loadTransactions = useCallback(async (p: number) => {
    // Omit period_start until refs have loaded a real billing period.
    // /api/v1/budgets and /api/v1/forecast-plans/current both resolve to
    // the current open period when period_start is absent — and the
    // strict resolver rejects calendar-month dates that don't match a
    // BillingPeriod row (salary-cycle orgs start mid-month).
    const budgetUrl = realPeriodStart ? `/api/v1/budgets?period_start=${realPeriodStart}` : "/api/v1/budgets";
    const forecastUrl = realPeriodStart ? `/api/v1/forecast-plans/current?period_start=${realPeriodStart}` : "/api/v1/forecast-plans/current";
    const [pageData, bds, fc] = await Promise.all([
      apiFetch<{ items: Transaction[]; total: number }>(`/api/v1/transactions?limit=${PAGE_SIZE}&offset=${p * PAGE_SIZE}${txQueryTail}`),
      p === 0 && !budgetsDisabled ? apiFetch<Budget[]>(budgetUrl) : null,
      // TBD-197: the forecast-plan call sits INSIDE this Promise.all too, so a
      // 404 from a gated route replaces the whole page with "Failed to load
      // dashboard data" rather than degrading one tile. Fenced by G6.
      p === 0 && !forecastDisabled ? apiFetch<ForecastPlan | null>(forecastUrl) : null,
    ]);
    const page_txs = pageData?.items ?? [];
    setTxTotal(pageData?.total ?? 0);
    setTransactions(page_txs);
    if (bds) setBudgets(bds);
    // null is a valid response (no plan yet) — set state so empty-state UI renders.
    if (p === 0) setForecast(fc ?? null);
    setFetching(false);
  }, [txQueryTail, realPeriodStart, budgetsDisabled, forecastDisabled]);

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
    // TBD-197 — load-bearing. With Forecast gated off `/api/v1/forecast` 404s
    // and the catch below sets `projectionFailed = true`, which OnTrackTile
    // renders as an error with a Retry button. A deliberate org setting must
    // never render as a failure. (The tile itself is hidden below; this keeps
    // the state clean regardless of render order.)
    if (forecastDisabled) {
      projectionRequestId.current += 1;
      setForecastProjection(null);
      setProjectionFailed(false);
      setProjectionLoading(false);
      return;
    }
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
  }, [realPeriodStart, forecastDisabled]);

  // The Spending donut's rollup (TBD-221).
  //
  // ⚠⚠ NO `forecastDisabled` GUARD, AND THAT IS THE POINT OF THE TICKET.
  // `/api/v1/transactions/spending-by-category` is a historical-actuals rollup
  // on the transactions router. It is ungated server-side and answers 200 for
  // an org that has Forecast switched off — which is exactly the org that used
  // to see "No expense data yet" over a period holding real settled expense.
  // The sibling loadForecastProjection above DOES carry the guard because its
  // endpoint genuinely 404s; do not "make them consistent".
  const loadSpendingRollup = useCallback(async () => {
    if (!realPeriodStart) {
      rollupRequestId.current += 1;
      setSpendingRollup(null);
      setRollupFailed(false);
      setRollupFetching(false);
      return;
    }
    const myId = ++rollupRequestId.current;
    // ⚠ NO `setSpendingRollup(null)` HERE — the last good payload, and the
    // analysis window an open drilldown runs against, stay until a newer one
    // lands or the period changes.
    setRollupFailed(false);
    setRollupFetching(true);
    try {
      // `period_start` is a HINT. The window comes back off the response.
      const data = await apiFetch<SpendingByCategoryResponse>(
        `/api/v1/transactions/spending-by-category?period_start=${realPeriodStart}`,
      );
      if (rollupRequestId.current !== myId) return;
      setSpendingRollup({ periodStart: realPeriodStart, data });
      setRollupFailed(false);
    } catch {
      if (rollupRequestId.current !== myId) return;
      // The stored rollup is deliberately LEFT ALONE: `rollupFailed` already
      // stops the tile rendering stale numbers, and dropping the payload would
      // destroy the window — and any open drilldown with it.
      setRollupFailed(true);
    } finally {
      if (rollupRequestId.current === myId) {
        setRollupFetching(false);
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- surfaces a reference-data fetch failure into the error banner state
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch loading flag raised before the period-scoped transaction load
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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the forecast projection fetch that loads its result into state
      void loadForecastProjection();
    }
  }, [loading, user, realPeriodStart, loadForecastProjection]);

  // Its own effect, not a line inside the projection's: the two must be able
  // to fail independently.
  useEffect(() => {
    if (!loading && user && realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the spending-by-category fetch that loads its result into state
      void loadSpendingRollup();
    }
  }, [loading, user, realPeriodStart, loadSpendingRollup]);

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
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the per-account month-end forecast fetch that loads its result into state
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
      loadSpendingRollup(),
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
    loadSpendingRollup,
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

  // Pending totals per account, computed from the all-time pending fetch
  // (NOT from the period-filtered transaction page). Pending CC charges
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

  // ── Spending by category: the SERVER rollup (TBD-221) ─────────────────────
  //
  // `spendingRollup.categories` is grouped in SQL, uncapped, and filtered with
  // `reportable_transaction_filter()` — the same clause the budget bars beside
  // it use. The memo this replaced re-derived the figure from a `limit=200`
  // page of raw rows filtered only on `linked_transaction_id == null`, so it
  // (a) counted manual balance adjustments and reverted reconciliation rows
  // that no other tile counted, (b) used a client calendar window, and (c)
  // dropped the oldest rows of any period past 200.
  //
  // `executed` is SETTLED expense — exactly what the donut has always shown.
  //
  // ⚠ THE SOURCE IS THE UNGATED ENDPOINT, NOT `forecastProjection.categories`.
  // Both payloads carry the same per-category numbers, so reading the
  // forecast's copy compiles and looks right — and blanks the tile for every
  // org that has Forecast switched off.
  //
  // ⚠ NO CLIENT FALLBACK. When the rollup is absent this is empty and the tile
  // renders its rollup-failed state. Re-aggregating here instead would silently
  // substitute the wrong number, which IS the defect being deleted.
  //
  // ⚠ TBD-309 REMOVED THE OLD REASON WITHOUT WEAKENING THE RULE, and the
  // distinction matters. This note used to say a client reconstruction "can
  // only ever be half the filter" because `reconciliation_state` was not on
  // the wire. `is_reverted` now is — and with it, ALL THREE columns of
  // `reportable_transaction_filter` are on the wire
  // (`linked_transaction_id`, `is_manual_adjustment`, `is_reverted`), so the
  // row-level predicate IS now exactly reconstructible here. That argument is
  // spent; do not reach for it.
  //
  // The rule survives on (b) and (c) above, which no wire field can fix: this
  // client holds ONE PAGE, not the period's row set, so any period past the
  // page size silently loses its oldest rows; and it would bucket by a client
  // calendar window rather than `effective_period_date_expr` against the org's
  // billing-period boundaries. Those are aggregation errors, not filter
  // errors, and they are invisible in exactly the way (a) was.
  //
  // donutData drives both the donut chart (always rendered in amount-desc
  // order so the largest slice starts at 12 o'clock) and the legend list
  // (sortable by name | percent | amount, persisted via spendingSort).
  const donutDataRaw = useMemo(() => {
    // `activeRollup`, not `spendingRollup`: a payload fetched for a period the
    // user has since navigated away from must not render under the new one.
    const rows = activeRollup?.categories;
    if (!Array.isArray(rows)) return [];
    return rows
      .map((r) => ({
        categoryId: r.category_id,
        name: r.category_name,
        value: Number(r.executed),
      }))
      // A defensive finite/positive guard, NOT a negative-expense handler: no
      // reachable state produces one. `TransactionCreate/Update.amount` and
      // `ImportConfirmRow.amount` are `Field(gt=0)`, the OFX path writes
      // `amount_abs`, sign is carried by `type`, and the rollup groups only
      // `type == EXPENSE` — a refund is an INCOME row, never a negative EXPENSE.
      .filter((d) => Number.isFinite(d.value) && d.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [activeRollup]);
  const donutData = donutDataRaw;
  // The sum of the RENDERED slices, so the percentages below add to 100 by
  // construction. Server-side `executed_expense` is itself the sum of the same
  // rows and agrees with it; what must never back this figure is a SECOND
  // source — a client re-aggregation, or
  // `forecastProjection.executed_expense`, which is null for a forecast-off org.
  const totalSpend = useMemo(
    () => donutDataRaw.reduce((s, d) => s + d.value, 0),
    [donutDataRaw],
  );
  const sortedSpending = useMemo(() => {
    const list = donutDataRaw.map((d, i) => ({
      categoryId: d.categoryId,
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

  // All budgets feed the "Budget Progress" mini bar chart on the dashboard.
  // Memoizing prevents Recharts from re-laying out the bars every time an
  // unrelated piece of dashboard state (sort toggle, expansion, hover)
  // re-renders the parent.
  //
  // Defensive Array.isArray guard: some API responses return objects on
  // empty/error paths, and the chart is only rendered when budgets is
  // actually populated — we just don't want this hoisted memo to throw
  // before that conditional renders.
  const dashBudgets = useMemo(
    () => (Array.isArray(budgets) ? budgets : []),
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

  // All expense items feed the "Forecast by Category" mini bar chart. Same
  // memoization rationale as the donut and budget charts.
  const forecastExpenseItems = useMemo(
    () => forecast?.items.filter((it) => it.type === "expense") ?? [],
    [forecast],
  );
  const forecastChartRows = useMemo(
    () =>
      forecastExpenseItems.map((it) => ({
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

  // ── Chart-filter label (TBD-221) ──────────────────────────────────────────
  // The badge reads its name off the rollup rows already in memory, so the
  // label and the slice it describes have ONE source of truth. Budget and
  // Forecast bars can filter a category with no settled spend this period and
  // therefore no rollup row, so those two in-memory lists back the LABEL up;
  // the NUMBER always comes from the server. Two same-named subcategories
  // rendering one label is deliberately TBD-326.
  const chartFilterName = useMemo(() => {
    if (chartFilter === null) return null;
    return (
      donutDataRaw.find((d) => d.categoryId === chartFilter)?.name
      ?? dashBudgets.find((b) => b.category_id === chartFilter)?.category_name
      ?? forecastExpenseItems.find((it) => it.category_id === chartFilter)?.category_name
      ?? null
    );
  }, [chartFilter, donutDataRaw, dashBudgets, forecastExpenseItems]);


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

  // The rendered list is the page the server returned — filtered or not.
  //
  // TBD-221 deleted the `tx.category_name === chartFilter` predicate that used
  // to live here: it compared on a name (not the rollup's identity) over a
  // capped snapshot (so it could not see rows past 200), which is how the list
  // could disagree with the slice that opened it. The server filters now.
  //
  // ⚠ Copy before sorting. `transactions` is state and `Array.prototype.sort`
  // mutates in place; the deleted `.filter()` used to hand `.sort()` a fresh
  // array. NO client-side dedupe either: the unfiltered page passes
  // collapse_transfers=true and the filtered one passes reportable=true, so the
  // server folded/dropped transfer legs BEFORE the limit (TBD-268).
  const sortedVisibleTxs = useMemo(
    () =>
      [...transactions]
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
    [transactions, dashSortField, dashSortDir],
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
            // Distinguishing name: the donut and the OnTrackTile each render
            // their own "Retry" on this page, and three identically named
            // buttons leave a screen-reader user no way to tell which is which.
            aria-label="Retry the dashboard refresh"
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
              <button onClick={() => { setPeriodIdx(Math.min(periodIdx + 1, periods.length - 1)); setChartFilterId(null); }} disabled={periodIdx >= periods.length - 1} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30" aria-label="Previous period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>
              </button>
              <span className="text-sm font-medium text-text-primary">
                {monthFrom}{monthTo ? ` – ${monthTo}` : ""}
              </span>
              <button onClick={() => { setPeriodIdx(Math.max(periodIdx - 1, 0)); setChartFilterId(null); }} disabled={periodIdx <= 0} className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30" aria-label="Next period">
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>
              </button>
              {isCurrentSelectedPeriod && <span className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success">CURRENT</span>}
              {!isCurrentSelectedPeriod && (
                <button onClick={() => { const idx = selectCurrentPeriodIndex(periods); if (idx >= 0) { setPeriodIdx(idx); setChartFilterId(null); } }} className="ml-1 inline-flex min-h-[44px] items-center rounded-md px-3 text-xs font-medium text-text-muted hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">Today</button>
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
          {/* On Track hero — hidden outright when the org switched Forecast
              off (TBD-197). Its empty states read "No plan for this period.
              Set one up →", which is both wrong and unactionable here: there
              is no plan because the tool is off, and the link it offers now
              lands on the disabled notice. The AI refine toggle nested inside
              goes with it — its endpoint is gated too, and it hides only on a
              403, not on the 404 the product gate returns. */}
          {!forecastDisabled && (
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
          )}

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
                        const idx = selectCurrentPeriodIndex(periods);
                        if (idx >= 0) {
                          setPeriodIdx(idx);
                          // Raw setter: period nav deliberately does NOT reset
                          // the recent-tx page (fenced).
                          setChartFilterId(null);
                        }
                      }}
                    />
                    <div className="absolute right-3 top-3">
                      <HelpTooltip
                        content="Each account's current balance plus everything still expected in this billing period: pending transactions, projected card and loan payments, and upcoming recurring activity."
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
            <div className={`${card} p-5`} data-testid="spending-donut">
              <h2 className={`mb-3 ${cardTitle}`}>Spending by Category</h2>
              {chartFilter !== null && (
                <button onClick={() => setChartFilter(null)} className="mb-2 rounded-md bg-surface-overlay px-2.5 py-1 text-xs text-text-secondary hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">
                  {/* The fallback is reachable: a category can be filtered from
                      the Budget or Forecast bars with no rollup row, no budget
                      and no forecast item to name it. Plain language, not
                      "selected category" — the user picked it, and a label that
                      reads like system vocabulary tells them the app lost
                      track. */}
                  Filtering: {chartFilterName ?? "one category"} &times;
                </button>
              )}
              {/* TBD-221: the numbers on this tile come from the UNGATED
                  /api/v1/transactions/spending-by-category endpoint. When that
                  call failed there is no number to show, and the tile says so
                  rather than falling back to a client aggregation.

                  ⚠ `rollupFailed`, NOT `projectionFailed`. The latter belongs
                  to /api/v1/forecast — a different, feature-gated request —
                  and reading it here renders "unavailable" over real settled
                  expense for any org with Forecast switched off. */}
              {rollupFailed ? (
                /* role="alert": this appears asynchronously, long after the
                   page has settled, so without a live region a screen-reader
                   user is told nothing at all (WCAG 2.2 AA 4.1.3). The Retry
                   button's accessible name says WHICH retry it is — three
                   buttons on this page are otherwise all called "Retry". */
                <div
                  role="alert"
                  className="flex flex-wrap items-center gap-3 py-6 text-sm text-text-muted"
                >
                  <span>Spending by category unavailable.</span>
                  <button
                    type="button"
                    onClick={loadSpendingRollup}
                    disabled={rollupLoading}
                    aria-label="Retry loading spending by category"
                    className={`${btnSecondary} text-xs disabled:opacity-50`}
                  >
                    <RefreshCw className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
                    Retry
                  </button>
                </div>
              ) : donutData.length > 0 ? (
                <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
                  <div className="h-40 w-40 shrink-0">
                    <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                      <PieChart>
                        <Pie
                          data={donutData} cx="50%" cy="50%" innerRadius={35} outerRadius={65}
                          paddingAngle={2} dataKey="value" stroke="none" cursor="pointer"
                          onClick={(_, idx) => {
                            // Keyed by category_id (the rollup's identity),
                            // never by name — names are not unique across
                            // subcategories.
                            const cid = donutData[idx]?.categoryId;
                            if (cid != null) setChartFilter(chartFilter === cid ? null : cid);
                          }}
                        >
                          {donutData.map((d, i) => (
                            <Cell key={d.categoryId} fill={CHART_SERIES[i % CHART_SERIES.length]}
                              opacity={chartFilter !== null && chartFilter !== d.categoryId ? 0.3 : 1} />
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
                        Pressable-Surfaces Rule in docs/design/DESIGN.md. */}
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
                      <button key={d.categoryId} onClick={() => setChartFilter(chartFilter === d.categoryId ? null : d.categoryId)}
                        className={`grid w-full grid-cols-[auto_minmax(0,1fr)_3rem_auto] items-center gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-surface-raised ${chartFilter === d.categoryId ? "bg-surface-overlay" : ""}`}>
                        <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CHART_SERIES[d.origIdx % CHART_SERIES.length] }} />
                        <span className="min-w-0 truncate text-left text-xs text-text-secondary">{d.name}</span>
                        {/* %/amount carry data, so they ride text-secondary
                            rather than the dimmer text-muted. */}
                        <span className="text-right text-[10px] tabular-nums text-text-secondary">{d.pct.toFixed(0)}%</span>
                        <span className="text-right text-xs tabular-nums text-text-secondary">{formatAmount(d.value)}</span>
                      </button>
                    ))}
                    {sortedSpending.length > 10 && (
                      <p className="px-1.5 text-[10px] text-text-muted">+{sortedSpending.length - 10} more (click chart to filter)</p>
                    )}
                  </div>
                </div>
              ) : rollupLoading ? (
                /* ⚠ AHEAD OF THE EMPTY STATE, BEHIND THE CHART. Without this
                   arm the tile renders "No expense data yet" over every cold
                   load and every period change — the exact sentence TBD-221
                   exists to stop showing over a period holding real settled
                   expense. It sits behind the chart arm so a same-period
                   refetch keeps the last good slices on screen instead of
                   blinking to a spinner. */
                <p
                  role="status"
                  data-testid="donut-loading"
                  className="text-sm text-text-muted py-6 text-center"
                >
                  Loading spending by category…
                </p>
              ) : (
                <p className="text-sm text-text-muted py-6 text-center">No expense data yet</p>
              )}
            </div>

            {/* Budget progress — hidden outright when the org switched
                Budgets off (TBD-197). Its empty state reads "No budgets for
                this period. Add one", which is both wrong and unactionable
                here: there are no budgets because the tool is off, and the
                link it offers now lands on the disabled notice. */}
            {!budgetsDisabled && (
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
                          // TBD-221: the cross-tile filter is a category_id
                          // now, so the drilldown can reproduce the rollup's
                          // grouping.
                          const cid = dashBudgets[idx]?.category_id;
                          if (cid != null) setChartFilter(chartFilter === cid ? null : cid);
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
            )}

            {/* Forecast comparison — planned vs actual per category. Hidden
                when Forecast is off (TBD-197): all three of its empty states
                link to /forecast-plans, which now shows the notice. */}
            {!forecastDisabled && (
            <div className={`${card} overflow-hidden p-5`}>
              <h2 className={`mb-3 ${cardTitle}`}>Forecast by Category</h2>
              {(() => {
                if (forecast && forecastExpenseItems.length > 0) {
                  return (
                    <div className="w-full min-w-0" style={{ height: Math.max(forecastExpenseItems.length * 32, 100) }}>
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
                              // TBD-221: category_id, matching the rollup's
                              // identity.
                              const cid = forecastExpenseItems[idx]?.category_id;
                              if (cid != null) setChartFilter(chartFilter === cid ? null : cid);
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
            )}
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
                // TBD-268: the partner is never in the page after the server
                // collapse, so read its account name off the row itself.
                // Non-null only for a MUTUAL link, so this is the ONE transfer
                // signal the row renders from — amount styling included. The
                // raw `linked_transaction_id` also matches a one-way
                // reconciliation match, which would then render unsigned and
                // in accent while its subline said plain account name.
                const isPairedTransfer = tx.linked_account_name != null;
                const [fromAcct, toAcct] = tx.type === "expense"
                  ? [tx.account_name, tx.linked_account_name]
                  : [tx.linked_account_name, tx.account_name];
                const amountClass = `text-sm font-medium tabular-nums ${isPairedTransfer ? "text-info" : tx.type === "income" ? "text-success" : "text-danger"}`;
                const amountText = `${isPairedTransfer ? "" : tx.type === "income" ? "+" : "-"}${formatAmount(tx.amount)}`;
                const subline = isPairedTransfer ? (
                  <>{fromAcct} &rarr; {toAcct}</>
                ) : (
                  <>{tx.account_name} &middot; {tx.category_name}</>
                );
                const statusPill = !isPairedTransfer ? (
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
                        // A settled/pending toggle moves the row in and out of
                        // the donut's SETTLED bucket, so the rollup must
                        // refresh alongside the projection.
                        void loadSpendingRollup();
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
              {/* Keyed off the RENDERED list, not the raw page array: under a
                  chart filter the rendered source is the snapshot, so keying
                  off `transactions` could leave a card with zero rows and no
                  empty state — a blank tile. */}
              {sortedVisibleTxs.length === 0 && (
                <div className="px-5 py-6 text-center text-sm text-text-muted">
                  {!canAdd ? "Create accounts and categories first." : "No transactions this period."}
                </div>
              )}
            </div>
            {/* TBD-221: the pager no longer hides under a chart filter. The
                drilldown is a paginated SERVER query now, so hiding it would
                cap the slice's list at one page while its own total said
                otherwise — the same silent truncation this ticket removed. */}
            {(txTotal > PAGE_SIZE || page > 0) && (
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
