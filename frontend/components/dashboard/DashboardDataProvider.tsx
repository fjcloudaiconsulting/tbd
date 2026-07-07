"use client";

/**
 * DashboardDataProvider — scoped React context that owns the shared data
 * fetches for the three custom-dashboard finance tiles (OnTrack hero,
 * Accounts strip, AccountMonthEndForecast) and the period-navigation state.
 *
 * Phase 2b: adds period-snapshot transactions + budgets fetches, the chart
 * memos (donut/spending/budget/forecast), spendingSort, and chartFilter.
 *
 * The fetch logic is a faithful extraction of LegacyDashboard in
 * app/dashboard/page.tsx — same endpoints, same non-blocking projection
 * semantics, same stale-request guards, same pfv:transaction-added listener.
 *
 * SWR Phase 2 (final slice): accounts + billing periods now come from the
 * shared SWR hooks (bare-path keys, auth-gated). The period selection is
 * tracked by IDENTITY (start_date) and the visible index is derived from the
 * SWR periods list, so a background revalidation reconciles the index
 * declaratively instead of resetting the user's navigation — the old
 * imperative loadRefs snapped back to the current period on every post-write
 * refresh.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { apiFetch } from "@/lib/api";
import { fetchAll } from "@/lib/pagination";
import { formatLocalDate, projectedPeriodEnd, todayISO } from "@/lib/format";
import { useAuth } from "@/components/auth/AuthProvider";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useBillingPeriods } from "@/lib/hooks/use-billing-periods";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import {
  SORT_KEY_DASHBOARD_SPENDING,
  SORT_KEY_DASHBOARD_TRANSACTIONS,
} from "@/lib/hooks/persisted-keys";
import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";
import type { PersistedSort } from "@/lib/hooks/use-persisted-sort";
import type { Account, BillingPeriod, Budget, Transaction } from "@/lib/types";
import type {
  ForecastPlanLike,
  ForecastProjectionLike,
} from "@/components/dashboard/OnTrackTile";
import type { AccountMonthEndForecastResponse } from "@/components/dashboard/AccountMonthEndForecast";

// Recent-transactions tile page size (mirrors LegacyDashboard's PAGE_SIZE).
const PAGE_SIZE = 10;

// Stable empty-array fallbacks so the SWR loading state (data === undefined)
// doesn't hand a fresh [] to memos/effects on every render.
const EMPTY_ACCOUNTS: Account[] = [];
const EMPTY_PERIODS: BillingPeriod[] = [];

// ── Chart row types (mirror LegacyDashboard verbatim) ────────────────────────

export type SpendingSort = "name" | "percent" | "amount";

// Dashboard recent-transactions sort fields (mirror LegacyDashboard verbatim).
export type DashTxSort = "date" | "description" | "status" | "amount";

export interface DonutDatum {
  name: string;
  value: number;
}

export interface SortedSpendingRow {
  name: string;
  value: number;
  pct: number;
  origIdx: number;
}

export interface BudgetChartRow {
  name: string;
  spent: number;
  remaining: number;
  pct: number;
}

// ForecastPlanItem as returned by the API (amounts are strings at the wire
// level — mirrors the local interface in LegacyDashboard).
export interface ForecastPlanItem {
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

export interface ForecastChartRow {
  categoryId: number;
  name: string;
  planned: number;
  actual: number;
}

// ── Public interface ──────────────────────────────────────────────────────────

export interface DashboardData {
  accounts: Account[];
  activeAccounts: Account[];
  pendingByAccount: Record<number, number>;
  forecast: ForecastPlanLike | null;
  forecastProjection: ForecastProjectionLike | null;
  projectionFailed: boolean;
  projectionLoading: boolean;
  onRetryProjection: () => void;
  accountMonthEndForecast: AccountMonthEndForecastResponse | null;
  accountMonthEndForecastError: boolean;
  // period
  periods: BillingPeriod[];
  periodIdx: number;
  setPeriodIdx: (i: number) => void;
  selectedPeriod: BillingPeriod | null;
  isCurrentSelectedPeriod: boolean;
  isPastSelectedPeriod: boolean;
  isFutureSelectedPeriod: boolean;
  monthFrom: string;
  monthTo: string;
  jumpToCurrentPeriod: () => void;
  // chart data (Phase 2b)
  allTransactions: Transaction[];
  budgets: Budget[];
  dashBudgets: Budget[];
  budgetChartData: BudgetChartRow[];
  donutData: DonutDatum[];
  totalSpend: number;
  sortedSpending: SortedSpendingRow[];
  spendingSort: PersistedSort<SpendingSort>;
  toggleSpendingSort: (field: SpendingSort) => void;
  forecastExpenseItems: ForecastPlanItem[];
  forecastChartRows: ForecastChartRow[];
  chartFilter: string | null;
  setChartFilter: (c: string | null) => void;
  // recent transactions tile (Phase 2c)
  transactions: Transaction[];
  txTotal: number;
  page: number;
  setPage: (p: number) => void;
  pageSize: number;
  setPageSize: (n: number) => void;
  visibleTxs: Transaction[];
  sortedVisibleTxs: Transaction[];
  txMap: Map<number, Transaction>;
  dashSort: PersistedSort<DashTxSort>;
  toggleDashSort: (field: DashTxSort) => void;
  canAdd: boolean;
  onToggleTransactionStatus: (tx: Transaction) => Promise<void>;
  // status
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

// ── Internal types ────────────────────────────────────────────────────────────

// Local shape for the forecast-plan response — only the fields
// DashboardData consumers read (ForecastPlanLike extends total_planned_expense).
interface ForecastPlan extends ForecastPlanLike {
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

// Full projection shape from GET /api/v1/forecast?period_start=…
interface ForecastProjection extends ForecastProjectionLike {
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

// ── Context ───────────────────────────────────────────────────────────────────

const DashboardContext = createContext<DashboardData | null>(null);

export function useDashboard(): DashboardData {
  const ctx = useContext(DashboardContext);
  if (!ctx) {
    throw new Error(
      "useDashboard must be used within a DashboardDataProvider",
    );
  }
  return ctx;
}

// ── Provider ──────────────────────────────────────────────────────────────────

export function DashboardDataProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  // FIX 5: seed billingCycleDay from the authed user (same as LegacyDashboard)
  // so the initial monthTo calculation is correct before the settings load
  // resolves. `loading` feeds the SWR auth gate below.
  const { user, loading: authLoading } = useAuth();

  // ── Reference data via shared SWR hooks (SWR Phase 2) ───────────────────────
  // Accounts + billing periods come from the shared hooks (bare-path keys) so
  // every surface dedupes onto one cache entry. The `enabled` gate blocks the
  // fetch until auth resolves (null SWR key), so no request ever fires before
  // the bearer token exists (the auth-race 403 class). In production
  // CustomDashboard already holds this provider's mount until `user` is
  // present; the gate keeps the provider safe when mounted directly (tests,
  // future embeddings).
  const refsEnabled = !authLoading && !!user;
  const {
    data: accountsData,
    error: accountsError,
    mutate: mutateAccounts,
  } = useAccounts(refsEnabled);
  const {
    data: periodsData,
    error: periodsError,
    mutate: mutateBillingPeriods,
  } = useBillingPeriods(refsEnabled);
  const accounts = accountsData ?? EMPTY_ACCOUNTS;
  const periods = periodsData ?? EMPTY_PERIODS;

  // ── Non-SWR settings refs (dashboard-specific) ──────────────────────────────
  const [period, setPeriod] = useState<BillingPeriod | null>(null);
  const [billingCycleDay, setBillingCycleDay] = useState(
    user?.billing_cycle_day ?? 1,
  );

  // ── Period selection (identity-based) ───────────────────────────────────────
  // The user's explicit selection is stored as the period's start_date, NOT an
  // index. `null` = no explicit navigation yet → follow the current open
  // period. The visible index is DERIVED from the SWR periods list below, so
  // a background revalidation reconciles the index declaratively: the selected
  // period keeps its identity even if the list re-orders or grows, and only
  // when it disappears do we fall back to the current open period. (The old
  // imperative loadRefs reset the index to "current" on every refresh,
  // clobbering the user's navigation after each write.)
  const [selectedStart, setSelectedStart] = useState<string | null>(null);

  // ── Chart filter (cross-tile) ───────────────────────────────────────────────
  const [chartFilter, setChartFilter] = useState<string | null>(null);

  // ── Spending sort (persisted) ───────────────────────────────────────────────
  const spendingSort = usePersistedSort<SpendingSort>(
    SORT_KEY_DASHBOARD_SPENDING,
    "amount",
    "desc",
    ["name", "percent", "amount"] as const,
  );

  // ── Recent-transactions sort (persisted) ────────────────────────────────────
  const dashSort = usePersistedSort<DashTxSort>(
    SORT_KEY_DASHBOARD_TRANSACTIONS,
    "date",
    "desc",
    ["date", "description", "status", "amount"] as const,
  );

  // ── Forecast plan (current period) ─────────────────────────────────────────
  const [forecast, setForecast] = useState<ForecastPlan | null>(null);
  const forecastPlanRequestId = useRef(0);

  // ── Pending transactions ────────────────────────────────────────────────────
  const [pendingTransactions, setPendingTransactions] = useState<Transaction[]>([]);
  const pendingRequestId = useRef(0);

  // ── Period snapshot transactions (limit=200) ────────────────────────────────
  const [allTransactions, setAllTransactions] = useState<Transaction[]>([]);
  const snapshotRequestId = useRef(0);

  // ── Paginated period transactions (recent-tx tile, Phase 2c) ────────────────
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [txTotal, setTxTotal] = useState(0);
  const [page, setPage] = useState(0);
  // Page size is user-selectable on the recent-tx tile (10–100); changing it
  // resets to page 0. loadPageTransactions reads the current size.
  const [pageSize, setPageSizeState] = useState(PAGE_SIZE);
  const setPageSize = useCallback((n: number) => {
    setPageSizeState(n);
    setPage(0);
  }, []);
  const txPageRequestId = useRef(0);

  // ── Period-scoped budgets ───────────────────────────────────────────────────
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const budgetsRequestId = useRef(0);

  // ── Forecast projection ─────────────────────────────────────────────────────
  const [forecastProjection, setForecastProjection] =
    useState<ForecastProjection | null>(null);
  const [projectionFailed, setProjectionFailed] = useState(false);
  const [projectionLoading, setProjectionLoading] = useState(false);
  const projectionRequestId = useRef(0);

  // ── Account month-end forecast ──────────────────────────────────────────────
  const [accountMonthEndForecast, setAccountMonthEndForecast] =
    useState<AccountMonthEndForecastResponse | null>(null);
  const [accountMonthEndForecastError, setAccountMonthEndForecastError] =
    useState(false);
  const accountForecastRequestId = useRef(0);

  // ── Load / error state ──────────────────────────────────────────────────────
  // "Settled" = resolved OR errored (mirrors the transactions cold-mount fix,
  // #520): an errored refs request must not strand the dashboard on the
  // skeleton. auxSettled tracks the imperative settings load the same way.
  const accountsSettled =
    accountsData !== undefined || accountsError !== undefined;
  const periodsSettled = periodsData !== undefined || periodsError !== undefined;
  const [auxSettled, setAuxSettled] = useState(false);
  const [auxError, setAuxError] = useState<string | null>(null);
  // Defense in depth: a billing-periods request that never settles (a stalled
  // connection that neither resolves nor errors) must not strand the dashboard
  // forever. After a generous delay we render anyway — period-scoped tiles
  // show their empty/unavailable states, and if periods do eventually arrive
  // everything re-derives from the real list.
  const [periodsWaitElapsed, setPeriodsWaitElapsed] = useState(false);
  const periodsResolved = periodsSettled || periodsWaitElapsed;
  const loading = !(accountsSettled && periodsResolved && auxSettled);
  const refsError = accountsError ?? periodsError;
  const error =
    auxError ??
    (refsError
      ? refsError instanceof Error
        ? refsError.message
        : "Failed to load dashboard data"
      : null);

  // ── Period derivations ──────────────────────────────────────────────────────
  // periodIdx is derived from the identity-based selection: the selected
  // period is looked up by start_date in the (SWR-owned) periods list; if it
  // vanished — or the user never navigated — fall back to the current open
  // period (end_date === null), matching the legacy default.
  const periodIdx = useMemo(() => {
    if (periods.length === 0) return 0;
    if (selectedStart !== null) {
      const idx = periods.findIndex((p) => p.start_date === selectedStart);
      if (idx >= 0) return idx;
    }
    const currentIdx = periods.findIndex((p) => p.end_date === null);
    return currentIdx >= 0 ? currentIdx : 0;
  }, [periods, selectedStart]);

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : period;
  const realPeriodStart: string | null = selectedPeriod?.start_date ?? null;

  const _today = todayISO();
  const isCurrentSelectedPeriod = selectedPeriod?.end_date === null;
  const isPastSelectedPeriod = !!(
    selectedPeriod?.end_date && selectedPeriod.end_date < _today
  );
  const isFutureSelectedPeriod = !!(
    selectedPeriod && selectedPeriod.start_date > _today
  );

  const monthFrom =
    realPeriodStart ??
    formatLocalDate(
      new Date(new Date().getFullYear(), new Date().getMonth(), 1),
    );
  const monthTo =
    selectedPeriod?.end_date ??
    (monthFrom ? (projectedPeriodEnd(monthFrom, billingCycleDay) ?? "") : "");

  // ── setPeriodIdx (clamped) — clears chartFilter on period nav ──────────────
  // Records the SELECTION IDENTITY (start_date) of the clamped index; the
  // visible periodIdx re-derives from it, so navigation survives a background
  // periods revalidation.
  const setPeriodIdx = useCallback(
    (i: number) => {
      const clamped = Math.max(0, Math.min(i, periods.length - 1));
      setSelectedStart(periods[clamped]?.start_date ?? null);
      setChartFilter(null);
    },
    [periods],
  );

  // ── jumpToCurrentPeriod — clears chartFilter on period nav ─────────────────
  const jumpToCurrentPeriod = useCallback(() => {
    const idx = periods.findIndex((p) => p.end_date === null);
    if (idx >= 0) {
      setSelectedStart(periods[idx].start_date);
      setChartFilter(null);
    }
  }, [periods]);

  // ── loadTransactionSnapshot ─────────────────────────────────────────────────
  // Full-period snapshot: GET /api/v1/transactions?limit=200&date_from=…&date_to=…
  // Mirrors the `allData` fetch in LegacyDashboard.loadTransactions (page 0).
  // Gated on realPeriodStart; stale-request guard matches sibling loaders.
  const loadTransactionSnapshot = useCallback(async () => {
    if (!realPeriodStart) {
      snapshotRequestId.current += 1;
      setAllTransactions([]);
      return;
    }
    const myId = ++snapshotRequestId.current;
    const dateFilter = `date_from=${monthFrom}${monthTo ? `&date_to=${monthTo}` : ""}`;
    try {
      const data = await apiFetch<{ items: Transaction[]; total: number }>(
        `/api/v1/transactions?limit=200&${dateFilter}`,
      );
      if (snapshotRequestId.current !== myId) return;
      setAllTransactions(data?.items ?? []);
    } catch {
      if (snapshotRequestId.current !== myId) return;
      // Silent — keep last good snapshot on transient failures.
    }
  }, [realPeriodStart, monthFrom, monthTo]);

  // ── loadPageTransactions ────────────────────────────────────────────────────
  // Paginated period transactions for the recent-tx tile:
  // GET /api/v1/transactions?limit=PAGE_SIZE&offset=p*PAGE_SIZE&date_from=…
  // Mirrors the page-data half of LegacyDashboard.loadTransactions. The
  // snapshot (allTransactions), budgets, and forecast plan are loaded by
  // their own sibling loaders, so this loader is page-data only.
  // Gated on realPeriodStart; stale-request guard matches sibling loaders.
  const loadPageTransactions = useCallback(
    async (p: number) => {
      if (!realPeriodStart) {
        txPageRequestId.current += 1;
        setTransactions([]);
        setTxTotal(0);
        return;
      }
      const myId = ++txPageRequestId.current;
      const dateFilter = `date_from=${monthFrom}${monthTo ? `&date_to=${monthTo}` : ""}`;
      try {
        const data = await apiFetch<{ items: Transaction[]; total: number }>(
          `/api/v1/transactions?limit=${pageSize}&offset=${p * pageSize}&${dateFilter}`,
        );
        if (txPageRequestId.current !== myId) return;
        setTransactions(data?.items ?? []);
        setTxTotal(data?.total ?? 0);
      } catch {
        if (txPageRequestId.current !== myId) return;
        // Silent — keep last good page on transient failures.
      }
    },
    [realPeriodStart, monthFrom, monthTo, pageSize],
  );

  // ── loadBudgets ─────────────────────────────────────────────────────────────
  // Per-period budgets. When realPeriodStart is known, request that specific
  // period. Mirrors the budgetUrl fetch in LegacyDashboard.loadTransactions.
  // On a transient failure, keep the last good budgets (don't blank them).
  // Stale-request guard matches sibling loaders.
  const loadBudgets = useCallback(async () => {
    const myId = ++budgetsRequestId.current;
    const budgetUrl = realPeriodStart
      ? `/api/v1/budgets?period_start=${realPeriodStart}`
      : "/api/v1/budgets";
    try {
      const bds = await apiFetch<Budget[]>(budgetUrl);
      if (budgetsRequestId.current !== myId) return;
      setBudgets(bds ?? []);
    } catch {
      if (budgetsRequestId.current !== myId) return;
      // Silent — keep last good budgets on transient failures.
    }
  }, [realPeriodStart]);

  // ── loadAux ─────────────────────────────────────────────────────────────────
  // The non-SWR settings refs. Accounts + billing periods moved to the shared
  // SWR hooks above (SWR Phase 2); categories were already dropped (FIX 7);
  // budgets are loaded per-period in loadBudgets (Phase 2b). What remains is
  // the current-period fallback + billing cycle day, both dashboard-specific
  // settings lookups.
  const loadAux = useCallback(async () => {
    const [per, bc] = await Promise.all([
      apiFetch<BillingPeriod>("/api/v1/settings/billing-period"),
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle"),
    ]);
    if (bc) setBillingCycleDay(bc.billing_cycle_day);
    if (per) setPeriod(per);
  }, []);

  // ── loadPendingTransactions ─────────────────────────────────────────────────
  const loadPendingTransactions = useCallback(async () => {
    const myId = ++pendingRequestId.current;
    try {
      const all = await fetchAll<Transaction>("/api/v1/transactions?status=pending");
      if (pendingRequestId.current !== myId) return;
      setPendingTransactions(all);
    } catch {
      // Silent — keep last good snapshot.
    }
  }, []);

  // ── loadForecastProjection ──────────────────────────────────────────────────
  const loadForecastProjection = useCallback(async () => {
    if (!realPeriodStart) {
      projectionRequestId.current += 1;
      setForecastProjection(null);
      setProjectionFailed(false);
      setProjectionLoading(false);
      return;
    }
    const myId = ++projectionRequestId.current;
    setForecastProjection(null);
    setProjectionFailed(false);
    setProjectionLoading(true);
    try {
      const projection = await apiFetch<ForecastProjection>(
        `/api/v1/forecast?period_start=${realPeriodStart}`,
      );
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

  // ── loadAccountMonthEndForecast ─────────────────────────────────────────────
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

  // ── loadForecastPlan ────────────────────────────────────────────────────────
  // Fetch the current forecast plan for the selected period (equivalent to
  // the page-0 loadTransactions call in LegacyDashboard). Done separately
  // so the provider doesn't need to load transactions.
  // FIX 3: monotonic stale-request guard + try/catch matching sibling loaders.
  const loadForecastPlan = useCallback(async () => {
    const myId = ++forecastPlanRequestId.current;
    const forecastUrl = realPeriodStart
      ? `/api/v1/forecast-plans/current?period_start=${realPeriodStart}`
      : "/api/v1/forecast-plans/current";
    try {
      const fc = await apiFetch<ForecastPlan | null>(forecastUrl);
      if (forecastPlanRequestId.current !== myId) return;
      setForecast(fc ?? null);
    } catch {
      if (forecastPlanRequestId.current !== myId) return;
      // Silent — keep last good snapshot on transient failures.
      setForecast(null);
    }
  }, [realPeriodStart]);

  // ── Initial load ────────────────────────────────────────────────────────────
  // Accounts + billing periods auto-fetch via the SWR hooks once refsEnabled
  // flips true; only the imperative loads remain here, gated the same way.
  useEffect(() => {
    if (!refsEnabled) return;
    loadAux()
      .catch((err: unknown) => {
        const msg =
          err instanceof Error ? err.message : "Failed to load dashboard data";
        setAuxError(msg);
      })
      .finally(() => setAuxSettled(true));
    void loadPendingTransactions();
  }, [refsEnabled, loadAux, loadPendingTransactions]);

  // Arm the stalled-periods fallback only while we are actually waiting
  // (same 10s bound as the transactions page, #520).
  useEffect(() => {
    if (!refsEnabled || periodsSettled) return;
    const timer = setTimeout(() => setPeriodsWaitElapsed(true), 10000);
    return () => clearTimeout(timer);
  }, [refsEnabled, periodsSettled]);

  // ── Period-scoped loads (fire when realPeriodStart is known) ────────────────
  useEffect(() => {
    if (realPeriodStart) {
      void loadForecastProjection();
    }
  }, [realPeriodStart, loadForecastProjection]);

  // FIX 4: gate account-forecast fetch on realPeriodStart being resolved,
  // matching the guard pattern on the sibling loadForecastProjection effect.
  useEffect(() => {
    if (realPeriodStart) {
      void loadAccountMonthEndForecast();
    }
  }, [realPeriodStart, loadAccountMonthEndForecast]);

  useEffect(() => {
    if (realPeriodStart) {
      void loadForecastPlan();
    }
  }, [realPeriodStart, loadForecastPlan]);

  // Phase 2b: period snapshot + budgets fire once realPeriodStart resolves.
  useEffect(() => {
    if (realPeriodStart) {
      void loadTransactionSnapshot();
    }
  }, [realPeriodStart, loadTransactionSnapshot]);

  // Budgets fire once the periods request has SETTLED (or the stall fallback
  // elapsed) rather than on a bare mount fetch + a period-scoped refetch: the
  // pre-SWR shape issued the request twice on every cold mount (once without
  // period_start, then again with it). loadBudgets already falls back to the
  // bare URL (current-period default) when realPeriodStart is still null, so
  // an errored periods request degrades to the legacy behavior instead of
  // never loading budgets at all.
  useEffect(() => {
    if (periodsResolved) {
      void loadBudgets();
    }
  }, [periodsResolved, loadBudgets]);

  // Phase 2c: the paginated recent-tx page re-fetches when the period OR the
  // page changes. Period nav does NOT reset the page (mirrors LegacyDashboard).
  useEffect(() => {
    if (realPeriodStart) {
      void loadPageTransactions(page);
    }
  }, [realPeriodStart, page, loadPageTransactions]);

  // ── refresh (post-write) ────────────────────────────────────────────────────
  // Mirrors LegacyDashboard.refreshAllPostWrite: the paginated page resets to
  // page 0 data (loadPageTransactions(0)) without mutating the `page` state,
  // matching legacy's loadTransactions(0) call there.
  //
  // SWR refs revalidate via their bound mutate(). Note a bare mutate()
  // revalidation SWALLOWS fetch errors (verified against SWR 2.4.1) — that is
  // fine here because refresh() deliberately ignores individual failures
  // (each loader keeps its last good snapshot; there is no refresh-error
  // banner on this surface). The identity-based periodIdx derivation keeps
  // the user's selected period stable across the periods revalidation.
  const refresh = useCallback(async () => {
    await Promise.allSettled([
      mutateAccounts(),
      mutateBillingPeriods(),
      loadAux(),
      loadForecastProjection(),
      loadPendingTransactions(),
      loadAccountMonthEndForecast(),
      loadForecastPlan(),
      loadTransactionSnapshot(),
      loadBudgets(),
      loadPageTransactions(0),
    ]);
  }, [
    mutateAccounts,
    mutateBillingPeriods,
    loadAux,
    loadForecastProjection,
    loadPendingTransactions,
    loadAccountMonthEndForecast,
    loadForecastPlan,
    loadTransactionSnapshot,
    loadBudgets,
    loadPageTransactions,
  ]);

  // ── pfv:transaction-added listener ─────────────────────────────────────────
  useTransactionAddedListener(() => {
    void refresh();
  });

  // ── Derived values ──────────────────────────────────────────────────────────
  const activeAccounts = useMemo(
    () => accounts.filter((a) => a.is_active),
    [accounts],
  );

  const pendingByAccount = useMemo(
    () =>
      pendingTransactions.reduce<Record<number, number>>((acc, tx) => {
        const sign = tx.type === "income" ? 1 : -1;
        acc[tx.account_id] = (acc[tx.account_id] ?? 0) + Number(tx.amount) * sign;
        return acc;
      }, {}),
    [pendingTransactions],
  );

  // ── Chart memos (copied verbatim from LegacyDashboard) ──────────────────────

  // Spending by category from all period transactions. Transfer expense
  // halves carry linked_transaction_id; excluding them here stops transfers
  // from polluting the Spending by Category donut.
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

  const totalSpend = useMemo(
    () => donutDataRaw.reduce((s, d) => s + d.value, 0),
    [donutDataRaw],
  );

  const sortedSpending = useMemo(() => {
    const list = donutDataRaw.map((d, i) => ({
      name: d.name,
      value: d.value,
      pct: totalSpend > 0 ? (d.value / totalSpend) * 100 : 0,
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

  // First six budgets feed the "Budget Overview" mini bar chart.
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

  // First eight expense items feed the "Forecast by Category" mini bar chart.
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

  // ── toggleSpendingSort (mirrors LegacyDashboard verbatim) ────────────────────
  const { field: spendingSortField, dir: spendingSortDir, setSort: setSpendingSort } = spendingSort;
  const toggleSpendingSort = useCallback(
    (field: SpendingSort) => {
      if (spendingSortField === field) {
        setSpendingSort(
          field,
          spendingSortDir === "asc" ? "desc" : "asc",
        );
      } else {
        setSpendingSort(field, field === "name" ? "asc" : "desc");
      }
    },
    [spendingSortField, spendingSortDir, setSpendingSort],
  );

  // ── Recent-transactions memos (copied verbatim from LegacyDashboard) ────────

  // O(1) linked-transaction lookups for transfer leg rendering. Built from the
  // full-period snapshot so a transfer's other half resolves regardless of
  // which page is visible.
  const txMap = useMemo(
    () => new Map(allTransactions.map((tx) => [tx.id, tx])),
    [allTransactions],
  );

  // When a chart filter is active, show from the full snapshot; otherwise the
  // paginated page. Dedups transfer legs (keep the lower-id half).
  const visibleTxs = useMemo(() => {
    const txSource = chartFilter ? allTransactions : transactions;
    const hiddenIds = new Set<number>();
    for (const tx of txSource) {
      if (tx.linked_transaction_id && tx.id > tx.linked_transaction_id) {
        hiddenIds.add(tx.id);
      }
    }
    return txSource.filter((tx) => !hiddenIds.has(tx.id));
  }, [chartFilter, allTransactions, transactions]);

  const { field: dashSortField, dir: dashSortDir, setSort: setDashSort } = dashSort;

  const sortedVisibleTxs = useMemo(
    () =>
      visibleTxs
        .filter((tx) => !chartFilter || tx.category_name === chartFilter)
        .sort((a, b) => {
          let cmp = 0;
          if (dashSortField === "date") cmp = a.date.localeCompare(b.date);
          else if (dashSortField === "description")
            cmp = a.description.localeCompare(b.description);
          // Status sort is alphabetical on the enum value: "pending" < "settled"
          // so asc surfaces pending rows first, desc surfaces settled first.
          else if (dashSortField === "status")
            cmp = a.status.localeCompare(b.status);
          else if (dashSortField === "amount")
            cmp = Number(a.amount) - Number(b.amount);
          return dashSortDir === "asc" ? cmp : -cmp;
        }),
    [visibleTxs, chartFilter, dashSortField, dashSortDir],
  );

  const toggleDashSort = useCallback(
    (field: DashTxSort) => {
      if (dashSortField === field) {
        setDashSort(field, dashSortDir === "asc" ? "desc" : "asc");
      } else {
        // Default direction per field: date desc (newest first), description /
        // status asc (alphabetical: pending before settled), amount asc.
        setDashSort(field, field === "date" ? "desc" : "asc");
      }
    },
    [dashSortField, dashSortDir, setDashSort],
  );

  // canAdd gates the empty-state copy. LegacyDashboard also required categories,
  // but the provider intentionally dropped the categories fetch (FIX 7); active
  // accounts is a sufficient proxy for the "setup incomplete" vs "no data" copy.
  const canAdd = activeAccounts.length > 0;

  // ── onToggleTransactionStatus (close reproduction of legacy ordering) ──────
  // PUT the flipped status, then refresh in LegacyDashboard's order: page data
  // + refs awaited; on page 0 the snapshot/budgets/forecast plan refresh too
  // (so the donut/budget/forecast charts reflect the change), matching legacy
  // loadTransactions(0)'s internal p===0 cascade. One deliberate relaxation vs
  // legacy: legacy AWAITED that cascade (it lived inside loadTransactions's
  // Promise.all); here it's fire-and-forget (void) since the three GETs are
  // independent and each loader owns its stale-guard + try/catch — the end
  // state converges identically, only intermediate render order differs.
  // The pending/projection/account-forecast reloads also stay fire-and-forget.
  // Rethrows on PUT failure so the calling tile can surface it; a failure of
  // the post-PUT page re-GET is swallowed by loadPageTransactions (keeps the
  // last good page, same as the sibling loaders) and does NOT surface as a
  // toggle error, since the mutation itself already committed.
  //
  // Refs step: the SWR mutates swallow their own fetch errors (bare
  // revalidation), so only loadAux can reject here — mirroring the old
  // loadRefs rejection surface for the settings half.
  const onToggleTransactionStatus = useCallback(
    async (tx: Transaction) => {
      await apiFetch(`/api/v1/transactions/${tx.id}`, {
        method: "PUT",
        body: JSON.stringify({
          status: tx.status === "settled" ? "pending" : "settled",
        }),
      });
      await loadPageTransactions(page);
      // Page-0 chart cascade fires BEFORE the refs step (matching legacy,
      // where it ran inside loadTransactions(0) ahead of loadRefs). loadAux
      // has no internal try/catch and can reject; keeping the cascade ahead
      // of it means a transient refs blip after a committed PUT can't skip
      // the donut/budget/forecast refresh.
      if (page === 0) {
        void loadTransactionSnapshot();
        void loadBudgets();
        void loadForecastPlan();
      }
      await Promise.all([mutateAccounts(), mutateBillingPeriods(), loadAux()]);
      void loadForecastProjection();
      void loadAccountMonthEndForecast();
      // Independent of `page`: a toggle on page 2 still has to refresh the
      // accounts strip's pending totals.
      void loadPendingTransactions();
    },
    [
      page,
      loadPageTransactions,
      mutateAccounts,
      mutateBillingPeriods,
      loadAux,
      loadTransactionSnapshot,
      loadBudgets,
      loadForecastPlan,
      loadForecastProjection,
      loadAccountMonthEndForecast,
      loadPendingTransactions,
    ],
  );

  // ── Context value ───────────────────────────────────────────────────────────
  const value: DashboardData = {
    accounts,
    activeAccounts,
    pendingByAccount,
    forecast,
    forecastProjection,
    projectionFailed,
    projectionLoading,
    onRetryProjection: loadForecastProjection,
    accountMonthEndForecast,
    accountMonthEndForecastError,
    periods,
    periodIdx,
    setPeriodIdx,
    selectedPeriod: selectedPeriod ?? null,
    isCurrentSelectedPeriod,
    isPastSelectedPeriod,
    isFutureSelectedPeriod,
    monthFrom,
    monthTo,
    jumpToCurrentPeriod,
    // Phase 2b chart data
    allTransactions,
    budgets,
    dashBudgets,
    budgetChartData,
    donutData: donutDataRaw,
    totalSpend,
    sortedSpending,
    spendingSort,
    toggleSpendingSort,
    forecastExpenseItems,
    forecastChartRows,
    chartFilter,
    setChartFilter,
    // Phase 2c recent transactions
    transactions,
    txTotal,
    page,
    setPage,
    pageSize,
    setPageSize,
    visibleTxs,
    sortedVisibleTxs,
    txMap,
    dashSort,
    toggleDashSort,
    canAdd,
    onToggleTransactionStatus,
    loading,
    error,
    refresh,
  };

  return (
    <DashboardContext.Provider value={value}>
      {children}
    </DashboardContext.Provider>
  );
}
