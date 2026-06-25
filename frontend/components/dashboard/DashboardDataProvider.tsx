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
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import { SORT_KEY_DASHBOARD_SPENDING } from "@/lib/hooks/persisted-keys";
import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";
import type { PersistedSort } from "@/lib/hooks/use-persisted-sort";
import type { Account, BillingPeriod, Budget, Transaction } from "@/lib/types";
import type {
  ForecastPlanLike,
  ForecastProjectionLike,
} from "@/components/dashboard/OnTrackTile";
import type { AccountMonthEndForecastResponse } from "@/components/dashboard/AccountMonthEndForecast";

// ── Chart row types (mirror LegacyDashboard verbatim) ────────────────────────

export type SpendingSort = "name" | "percent" | "amount";

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
  // so the initial monthTo calculation is correct before loadRefs resolves.
  const { user } = useAuth();

  // ── Refs state ──────────────────────────────────────────────────────────────
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [period, setPeriod] = useState<BillingPeriod | null>(null);
  const [billingCycleDay, setBillingCycleDay] = useState(
    user?.billing_cycle_day ?? 1,
  );
  const [periodIdx, setPeriodIdxRaw] = useState(0);

  // ── Chart filter (cross-tile) ───────────────────────────────────────────────
  const [chartFilter, setChartFilter] = useState<string | null>(null);

  // ── Spending sort (persisted) ───────────────────────────────────────────────
  const spendingSort = usePersistedSort<SpendingSort>(
    SORT_KEY_DASHBOARD_SPENDING,
    "amount",
    "desc",
    ["name", "percent", "amount"] as const,
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ── Period derivations (mirrors LegacyDashboard verbatim) ──────────────────
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
  const setPeriodIdx = useCallback(
    (i: number) => {
      const clamped = Math.max(0, Math.min(i, periods.length - 1));
      setPeriodIdxRaw(clamped);
      setChartFilter(null);
    },
    [periods.length],
  );

  // ── jumpToCurrentPeriod — clears chartFilter on period nav ─────────────────
  const jumpToCurrentPeriod = useCallback(() => {
    const idx = periods.findIndex((p) => p.end_date === null);
    if (idx >= 0) {
      setPeriodIdxRaw(idx);
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

  // ── loadInitialBudgets ───────────────────────────────────────────────────────
  // One-shot mount fetch: no period_start → API resolves the current open period
  // by default. Mirrors LegacyDashboard which fetched budgets in loadRefs
  // (no period_start). Stable identity ([]) so it doesn't re-trigger the mount
  // effect when realPeriodStart resolves. The period-change effect below will
  // re-fetch with the resolved period_start once it becomes known.
  const loadInitialBudgets = useCallback(async () => {
    const myId = ++budgetsRequestId.current;
    try {
      const bds = await apiFetch<Budget[]>("/api/v1/budgets");
      if (budgetsRequestId.current !== myId) return;
      setBudgets(bds ?? []);
    } catch {
      if (budgetsRequestId.current !== myId) return;
      // Silent — keep last good budgets on transient failures.
    }
  }, []);

  // ── loadRefs ────────────────────────────────────────────────────────────────
  // FIX 7: categories removed — no chart tile needs them. Budgets are
  // loaded per-period in loadBudgets (Phase 2b), not as a ref here.
  const loadRefs = useCallback(async () => {
    const [accts, per, plist, bc] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<BillingPeriod>("/api/v1/settings/billing-period"),
      apiFetch<BillingPeriod[]>("/api/v1/settings/billing-periods"),
      apiFetch<{ billing_cycle_day: number }>("/api/v1/settings/billing-cycle"),
    ]);
    setAccounts(accts ?? []);
    if (bc) setBillingCycleDay(bc.billing_cycle_day);
    if (per) setPeriod(per);
    const pl = plist ?? [];
    setPeriods(pl);
    // Default to the current (open) period, not index 0.
    const currentIdx = pl.findIndex((p) => p.end_date === null);
    if (currentIdx >= 0) setPeriodIdxRaw(currentIdx);
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
  useEffect(() => {
    setLoading(true);
    loadRefs()
      .then(() => setLoading(false))
      .catch((err: unknown) => {
        const msg =
          err instanceof Error ? err.message : "Failed to load dashboard data";
        setError(msg);
        setLoading(false);
      });
    void loadPendingTransactions();
    // One-shot initial budget fetch (no period_start → current-period default),
    // mirroring LegacyDashboard which always fetched budgets in loadRefs.
    // Uses the stable loadInitialBudgets (no realPeriodStart dep) so this
    // effect doesn't re-run when the period resolves.
    void loadInitialBudgets();
  }, [loadRefs, loadPendingTransactions, loadInitialBudgets]);

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

  useEffect(() => {
    if (realPeriodStart) {
      void loadBudgets();
    }
  }, [realPeriodStart, loadBudgets]);

  // ── refresh (post-write) ────────────────────────────────────────────────────
  const refresh = useCallback(async () => {
    await Promise.allSettled([
      loadRefs(),
      loadForecastProjection(),
      loadPendingTransactions(),
      loadAccountMonthEndForecast(),
      loadForecastPlan(),
      loadTransactionSnapshot(),
      loadBudgets(),
    ]);
  }, [
    loadRefs,
    loadForecastProjection,
    loadPendingTransactions,
    loadAccountMonthEndForecast,
    loadForecastPlan,
    loadTransactionSnapshot,
    loadBudgets,
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
