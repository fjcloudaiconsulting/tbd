"use client";

/**
 * DashboardDataProvider — scoped React context that owns the shared data
 * fetches for the three custom-dashboard finance tiles (OnTrack hero,
 * Accounts strip, AccountMonthEndForecast) and the period-navigation state.
 *
 * Phase 2a subset only: refs + projection + account-forecast + pending txns
 * + period nav. Transactions table / chart memos / status-mutation are
 * deliberately absent (Phase 2b/2c).
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
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import type { Account, BillingPeriod, Transaction } from "@/lib/types";
import type {
  ForecastPlanLike,
  ForecastProjectionLike,
} from "@/components/dashboard/OnTrackTile";
import type { AccountMonthEndForecastResponse } from "@/components/dashboard/AccountMonthEndForecast";

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
  items: unknown[];
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
  // ── Refs state ──────────────────────────────────────────────────────────────
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [periods, setPeriods] = useState<BillingPeriod[]>([]);
  const [period, setPeriod] = useState<BillingPeriod | null>(null);
  const [billingCycleDay, setBillingCycleDay] = useState(1);
  const [periodIdx, setPeriodIdxRaw] = useState(0);

  // ── Forecast plan (current period) ─────────────────────────────────────────
  const [forecast, setForecast] = useState<ForecastPlan | null>(null);

  // ── Pending transactions ────────────────────────────────────────────────────
  const [pendingTransactions, setPendingTransactions] = useState<Transaction[]>([]);
  const pendingRequestId = useRef(0);

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

  // ── setPeriodIdx (clamped) ──────────────────────────────────────────────────
  const setPeriodIdx = useCallback(
    (i: number) => {
      const clamped = Math.max(0, Math.min(i, periods.length - 1));
      setPeriodIdxRaw(clamped);
    },
    [periods.length],
  );

  // ── jumpToCurrentPeriod ─────────────────────────────────────────────────────
  const jumpToCurrentPeriod = useCallback(() => {
    const idx = periods.findIndex((p) => p.end_date === null);
    if (idx >= 0) setPeriodIdxRaw(idx);
  }, [periods]);

  // ── loadRefs ────────────────────────────────────────────────────────────────
  const loadRefs = useCallback(async () => {
    const [accts, , , per, plist, bc] = await Promise.all([
      apiFetch<Account[]>("/api/v1/accounts"),
      apiFetch<unknown[]>("/api/v1/categories"),
      apiFetch<unknown[]>("/api/v1/budgets"),
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
  const loadForecastPlan = useCallback(async () => {
    const forecastUrl = realPeriodStart
      ? `/api/v1/forecast-plans/current?period_start=${realPeriodStart}`
      : "/api/v1/forecast-plans/current";
    const fc = await apiFetch<ForecastPlan | null>(forecastUrl);
    setForecast(fc ?? null);
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
  }, [loadRefs, loadPendingTransactions]);

  // ── Period-scoped loads (fire when realPeriodStart is known) ────────────────
  useEffect(() => {
    if (realPeriodStart) {
      void loadForecastProjection();
    }
  }, [realPeriodStart, loadForecastProjection]);

  useEffect(() => {
    void loadAccountMonthEndForecast();
  }, [loadAccountMonthEndForecast]);

  useEffect(() => {
    if (realPeriodStart) {
      void loadForecastPlan();
    }
  }, [realPeriodStart, loadForecastPlan]);

  // ── refresh (post-write) ────────────────────────────────────────────────────
  const refresh = useCallback(async () => {
    await Promise.allSettled([
      loadRefs(),
      loadForecastProjection(),
      loadPendingTransactions(),
      loadAccountMonthEndForecast(),
      loadForecastPlan(),
    ]);
  }, [
    loadRefs,
    loadForecastProjection,
    loadPendingTransactions,
    loadAccountMonthEndForecast,
    loadForecastPlan,
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
