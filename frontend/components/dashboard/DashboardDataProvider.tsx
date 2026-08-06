"use client";

/**
 * DashboardDataProvider — scoped React context that owns the shared data
 * fetches for the three custom-dashboard finance tiles (OnTrack hero,
 * Accounts strip, AccountMonthEndForecast) and the period-navigation state.
 *
 * Phase 2b: adds the budgets fetch, the chart memos
 * (donut/spending/budget/forecast), spendingSort, and chartFilter.
 *
 * TBD-221: the Spending donut reads `GET /api/v1/transactions/
 * spending-by-category` rather than aggregating a `limit=200` page of raw rows
 * client-side, and the chart filter is a category_id driving a server
 * drilldown rather than a category NAME filtering an in-memory snapshot. The
 * period snapshot fetch is gone with them.
 *
 * ⚠⚠ That endpoint is UNGATED and lives on the TRANSACTIONS router, and both
 * facts are load-bearing. The donut is a HISTORICAL ACTUALS tile; the earlier
 * draft of this change read the same rollup off `GET /api/v1/forecast`, which
 * TBD-197 lets an org switch off. An org with Forecast disabled then saw "No
 * expense data yet" over a period holding real settled expense. The donut's
 * data path therefore carries NO `forecastDisabled` guard — see
 * `loadSpendingRollup` below — and its failure flag (`rollupFailed`) is
 * SEPARATE from the projection's (`projectionFailed`). One flag for two
 * independent requests against two independent endpoints, one gated and one
 * not, means a forecast outage blanks a working donut and vice versa.
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
import {
  periodStatus,
  selectCurrentPeriod,
  selectCurrentPeriodIndex,
} from "@/lib/billingPeriodStatus";
import { useAuth } from "@/components/auth/AuthProvider";
import { useAccounts } from "@/lib/hooks/use-accounts";
import { useBillingPeriods } from "@/lib/hooks/use-billing-periods";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import {
  PAGE_SIZE_KEY_DASHBOARD_RECENT,
  SORT_KEY_DASHBOARD_SPENDING,
  SORT_KEY_DASHBOARD_TRANSACTIONS,
} from "@/lib/hooks/persisted-keys";
import { usePersistedSort } from "@/lib/hooks/use-persisted-sort";
import type { PersistedSort } from "@/lib/hooks/use-persisted-sort";
import { PAGE_SIZE_OPTIONS } from "@/lib/hooks/use-table-state";
import { readPersisted, writePersisted } from "@/lib/persisted-state";
import type { Account, BillingPeriod, Budget, Transaction } from "@/lib/types";
import type {
  ForecastPlanLike,
  ForecastProjectionLike,
} from "@/components/dashboard/OnTrackTile";
import type { AccountMonthEndForecastResponse } from "@/components/dashboard/AccountMonthEndForecast";

// Recent-transactions tile page size (mirrors LegacyDashboard's PAGE_SIZE).
const PAGE_SIZE = 10;

// Guard for the persisted recent-tx page size: only accept one of the
// selectable options so a stale / hand-edited localStorage value can never
// drive an off-menu limit. Keyed by surface only (flat, like the
// transactions page's PAGE_SIZE_KEY_TRANSACTIONS); per-user/org isolation is
// the separate localStorage-scope backlog item.
function isValidPageSize(value: unknown): value is number {
  return (
    typeof value === "number" &&
    (PAGE_SIZE_OPTIONS as readonly number[]).includes(value)
  );
}

// Stable empty-array fallbacks so the SWR loading state (data === undefined)
// doesn't hand a fresh [] to memos/effects on every render.
const EMPTY_ACCOUNTS: Account[] = [];
const EMPTY_PERIODS: BillingPeriod[] = [];

// ── Chart row types (mirror LegacyDashboard verbatim) ────────────────────────

export type SpendingSort = "name" | "percent" | "amount";

// Dashboard recent-transactions sort fields (mirror LegacyDashboard verbatim).
export type DashTxSort = "date" | "description" | "status" | "amount";

export interface DonutDatum {
  // TBD-221: the rollup's identity is the category id, and the drilldown
  // needs it. Names are NOT unique (two subcategories under different
  // masters may share one), which is why the name can no longer key a
  // filter, a React key, or a slice lookup.
  categoryId: number;
  name: string;
  value: number;
}

export interface SortedSpendingRow {
  categoryId: number;
  name: string;
  value: number;
  pct: number;
  origIdx: number;
}

/**
 * One row of `GET /api/v1/transactions/spending-by-category`
 * (`backend/app/services/spending_service.py`). Amounts are decimal strings on
 * the wire.
 *
 * `executed` is SETTLED, reportable EXPENSE — grouped in SQL, uncapped, and
 * filtered with `reportable_transaction_filter()`, the same clause every other
 * server aggregate on this screen applies. There is deliberately no `pending`
 * / `recurring` / `forecast` here: those are synthesized from templates that
 * have not materialised, which IS the Forecast product, and re-exporting them
 * would re-gate this surface by the back door.
 *
 * ⚠ Rows are keyed by the transaction's OWN `category_id`, so a master
 * category and its subcategory are two separate slices and `parent_id` carries
 * the link. That is why the drilldown must pass `category_match=exact` — see
 * `txQueryTail` below.
 */
export interface SpendingCategoryRow {
  category_id: number;
  category_name: string;
  parent_id: number | null;
  executed: string;
}

/**
 * `GET /api/v1/transactions/spending-by-category?period_start=…`.
 *
 * ⚠ `period_start` on the REQUEST is a hint, not a filter: a syntactically
 * valid value matching no BillingPeriod row is silently substituted with the
 * org's current period — no 404, no 422. `period_start` on this RESPONSE is
 * the period the server actually resolved, and it is the only one anything may
 * label, bound, or drill down with. `rollupFrom` / `rollupTo` below read it
 * back off here for exactly that reason.
 *
 * `executed_expense` is the sum of `categories[].executed` by construction
 * server-side, so the tile's total and its slices cannot disagree.
 */
export interface SpendingByCategoryResponse {
  period_start: string;
  period_end: string;
  executed_expense: string;
  categories: SpendingCategoryRow[];
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
  // ⚠ The projection flags below belong to `GET /api/v1/forecast` and to
  // OnTrackTile ALONE. The Spending donut has its own trio (`rollupFailed` /
  // `rollupLoading` / `onRetryRollup`) against its own ungated endpoint.
  // Collapsing the two back into one flag makes a forecast outage blank a
  // working donut and a rollup outage blank a working forecast (TBD-221).
  projectionFailed: boolean;
  projectionLoading: boolean;
  onRetryProjection: () => void;
  rollupFailed: boolean;
  rollupLoading: boolean;
  onRetryRollup: () => void;
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
  // TBD-221: a category_id, not a name. `chartFilterName` is the label for
  // it, looked up from the rollup rows already in memory.
  chartFilter: number | null;
  chartFilterName: string | null;
  setChartFilter: (c: number | null) => void;
  // recent transactions tile (Phase 2c)
  transactions: Transaction[];
  txTotal: number;
  page: number;
  setPage: (p: number) => void;
  pageSize: number;
  setPageSize: (n: number) => void;
  sortedVisibleTxs: Transaction[];
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
  // Still on the wire, still unread here. The donut's per-category numbers
  // come from the UNGATED spending-by-category endpoint instead — this
  // payload's copy is only reachable by orgs that have Forecast switched on,
  // which is precisely the coupling TBD-221 removes. `unknown[]` keeps it that
  // way: typing it would invite a reader.
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
  const { user, loading: authLoading, features } = useAuth();
  // TBD-197. `=== false`, never truthiness — `features` is undefined on a
  // booting client and in every pre-existing test mock, and Budgets ships ON.
  const budgetsDisabled = features?.budgets === false;
  // TBD-197 PR 2. Same `=== false` rule. Gates `loadForecastProjection` and
  // `loadForecastPlan` — and NOT `loadAccountMonthEndForecast`, whose endpoint
  // stays open server-side (see that loader for why).
  const forecastDisabled = features?.forecast === false;

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
  // TBD-221: a category_id. The old string filter compared `tx.category_name`
  // client-side, which is neither the rollup's identity nor unique.
  const [chartFilterId, setChartFilterId] = useState<number | null>(null);

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

  // ── Paginated period transactions (recent-tx tile, Phase 2c) ────────────────
  //
  // ⚠ TBD-221 deleted the sibling `limit=200` period snapshot that used to
  // live here. It had exactly two consumers — the donut memo and the
  // chart-filter branch of `visibleTxs` — and this change replaces both: the
  // donut reads the server rollup, and the drilldown is a server query. That
  // is how the 200-row cap dies, by removing the only fetch that had one
  // rather than by raising `le=200` on a PAT-reachable endpoint.
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [txTotal, setTxTotal] = useState(0);
  const [page, setPage] = useState(0);
  // Page size is user-selectable on the recent-tx tile (10–100); changing it
  // resets to page 0. loadPageTransactions reads the current size. Persisted
  // to localStorage (lazy one-shot read on mount, write-through on change) so
  // the chosen size survives reload and navigation, mirroring the sort hooks.
  const [pageSize, setPageSizeState] = useState<number>(() =>
    readPersisted(PAGE_SIZE_KEY_DASHBOARD_RECENT, PAGE_SIZE, isValidPageSize),
  );
  const setPageSize = useCallback((n: number) => {
    setPageSizeState(n);
    writePersisted(PAGE_SIZE_KEY_DASHBOARD_RECENT, n);
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

  // ── Spending-by-category rollup (the donut's source, TBD-221) ───────────────
  // Its OWN state and its OWN failure flag, deliberately not shared with the
  // projection above. Two endpoints, two lifecycles: `/api/v1/forecast` is
  // gated by TBD-197 and projects forward; this one is ungated and reports
  // what already happened.
  const [spendingRollup, setSpendingRollup] =
    useState<SpendingByCategoryResponse | null>(null);
  const [rollupFailed, setRollupFailed] = useState(false);
  const [rollupLoading, setRollupLoading] = useState(false);
  const rollupRequestId = useRef(0);

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
  // Defense in depth: a refs request that never settles (a stalled connection
  // that neither resolves nor errors) must not strand the dashboard forever.
  // The bound covers ALL loading legs — accounts, billing periods, AND the
  // settings aux load: after a generous delay we render anyway. Tiles show
  // their empty/unavailable states, and if the data does eventually arrive
  // everything re-derives from it.
  const refsSettled = accountsSettled && periodsSettled && auxSettled;
  const [refsWaitElapsed, setRefsWaitElapsed] = useState(false);
  const periodsResolved = periodsSettled || refsWaitElapsed;
  const loading = !refsSettled && !refsWaitElapsed;
  // NOTE: refsError is live SWR state — a failed BACKGROUND revalidation also
  // sets it while the last-good data keeps rendering. No production consumer
  // reads `error` today (only tests do); a future consumer must not render it
  // over still-valid data without checking `data !== undefined` first.
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
  // vanished — or the user never navigated — fall back to the current period
  // per `selectCurrentPeriod` (TBD-242), matching the legacy default.
  //
  // ⚠ The `-1` fallback to index 0 is the `no_open` roster: no row is open, so
  // there is no current period and the list's first row is shown. That is the
  // pre-existing behaviour, preserved deliberately; making it visible is
  // TBD-235's job, not this refactor's.
  const periodIdx = useMemo(() => {
    if (periods.length === 0) return 0;
    if (selectedStart !== null) {
      const idx = periods.findIndex((p) => p.start_date === selectedStart);
      if (idx >= 0) return idx;
    }
    const currentIdx = selectCurrentPeriodIndex(periods);
    return currentIdx >= 0 ? currentIdx : 0;
  }, [periods, selectedStart]);

  const selectedPeriod = periods.length > 0 ? periods[periodIdx] : period;
  const realPeriodStart: string | null = selectedPeriod?.start_date ?? null;

  // TBD-242: one classifier, one clock. `_today` is LOCAL (`todayISO`), never
  // UTC — Forecasts used to compute its own UTC today and disagree with this
  // file for any user east of Greenwich.
  const _today = todayISO();
  const selectedStatus = selectedPeriod
    ? periodStatus(selectedPeriod, _today)
    : null;
  const isCurrentSelectedPeriod = selectedStatus === "open";
  const isPastSelectedPeriod = selectedStatus === "past";
  const isFutureSelectedPeriod = selectedStatus === "upcoming";

  const monthFrom =
    realPeriodStart ??
    formatLocalDate(
      new Date(new Date().getFullYear(), new Date().getMonth(), 1),
    );
  // ⚠ DISPLAY WINDOW, NEVER AN ANALYSIS BOUND (TBD-221 / TBD-243). The open-
  // period arm is a client calendar formula; the server bounds every total on
  // this screen by `period_spend_window_end`. `monthTo` may only scope the
  // UNFILTERED Recent Transactions list, which is a ledger view and sums to
  // nothing. Anything that produces or drills into a number uses the window
  // the rollup shipped with (`rollupFrom` / `rollupTo` below).
  const monthTo =
    selectedPeriod?.end_date ??
    (monthFrom ? (projectedPeriodEnd(monthFrom, billingCycleDay) ?? "") : "");

  // ── The rollup's own analysis window (TBD-221) ──────────────────────────────
  // Off the SAME payload as the per-category totals, so window and numbers
  // arrive together and cannot drift.
  //
  // ⚠ READ BACK OFF THE RESPONSE, never `realPeriodStart`. `period_start` on
  // the request is a hint: a value matching no BillingPeriod row for the org is
  // silently substituted with the current period, with no 404 and no 422. A
  // drilldown built from the value we SENT would then query a window the
  // numbers above do not describe.
  const rollupFrom = spendingRollup?.period_start ?? null;
  const rollupTo = spendingRollup?.period_end ?? null;

  // A drilldown is only meaningful while that window is known: the filtered
  // query has to reproduce the slice's WHERE clause, and the window is half of
  // it. With no rollup (failed or still in flight) the filter reads null and
  // the tile falls back to the plain period page — rather than guessing a bound
  // from `monthTo`, which is the disagreement this ticket removes. Trade-off: a
  // rollup refetch blanks `spendingRollup` for a frame, so an active filter
  // drops and re-applies across a post-write refresh. That costs one extra list
  // GET and is self-healing; substituting a wrong window would not be.
  const chartFilter = rollupFrom && rollupTo ? chartFilterId : null;

  const setChartFilter = useCallback((c: number | null) => {
    setChartFilterId(c);
    // The drilldown is its own paginated result set — landing on page 4 of a
    // three-row slice would render an empty tile.
    setPage(0);
  }, []);

  // ── setPeriodIdx (clamped) — clears chartFilter on period nav ──────────────
  // Records the SELECTION IDENTITY (start_date) of the clamped index; the
  // visible periodIdx re-derives from it, so navigation survives a background
  // periods revalidation.
  const setPeriodIdx = useCallback(
    (i: number) => {
      const clamped = Math.max(0, Math.min(i, periods.length - 1));
      setSelectedStart(periods[clamped]?.start_date ?? null);
      // The raw setter, not `setChartFilter`: period nav deliberately does NOT
      // reset the recent-tx page (mirrors LegacyDashboard, fenced).
      setChartFilterId(null);
    },
    [periods],
  );

  // ── jumpToCurrentPeriod — clears chartFilter on period nav ─────────────────
  // Restores the never-navigated default (selectedStart = null → follow the
  // current open period) rather than pinning to the current period's
  // identity: a pin would park the user on the then-closed period after a
  // month rollover, the opposite of what "Today" means.
  const jumpToCurrentPeriod = useCallback(() => {
    if (selectCurrentPeriod(periods) !== null) {
      setSelectedStart(null);
      // Raw setter — see setPeriodIdx above.
      setChartFilterId(null);
    }
  }, [periods]);

  // If the selected period disappears from the list (the derivation above is
  // already rendering the current-period fallback), drop the pin too — a
  // later revalidation reintroducing the same start_date must not silently
  // resurrect a selection the user has visibly lost.
  useEffect(() => {
    if (selectedStart === null || periods.length === 0) return;
    if (!periods.some((p) => p.start_date === selectedStart)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- drop the period pin after a revalidation removes the selected period from the list
      setSelectedStart(null);
    }
  }, [periods, selectedStart]);

  // ── The recent-tx list query, in its two shapes (TBD-221) ───────────────────
  //
  // Plain string, deliberately not a memo: `useCallback` compares deps with
  // Object.is, and two equal strings are Object.is-equal. So the loader below
  // keeps a stable identity for as long as the URL it would build is
  // unchanged — which is what stops the projection landing (rollup window
  // null → known) from re-firing an identical unfiltered page fetch.
  //
  //  • UNFILTERED — the "Recent Transactions" ledger view over the DISPLAY
  //    window. It is a list, not a total; `monthTo` may bound it.
  //
  //  • FILTERED — the drilldown into one rollup slice. It reproduces that
  //    slice's WHERE clause exactly, so the list cannot show a row the slice
  //    excluded, nor sum past the number the user clicked.
  //
  //    ⚠ `category_match=exact` is NOT optional. `category_id` on the list
  //    endpoint is master-includes-subs (a 2026-05-13 regression guard) while
  //    the rollup groups by the row's OWN category_id. Without it a master
  //    slice opens a list summing to more than itself — silently, and only
  //    for orgs that put transactions directly on a master.
  //
  //    ⚠ `collapse_transfers` is deliberately ABSENT. `reportable=true`
  //    already excludes every non-null `linked_transaction_id`, a strict
  //    superset of it; sending both would be a contradiction in one URL.
  const txQueryTail =
    chartFilter !== null && rollupFrom && rollupTo
      ? `&category_id=${chartFilter}&category_match=exact&reportable=true` +
        `&type=expense&status=settled` +
        `&date_from=${rollupFrom}&date_to=${rollupTo}`
      : `&collapse_transfers=true&date_from=${monthFrom}` +
        `${monthTo ? `&date_to=${monthTo}` : ""}`;

  // ── loadPageTransactions ────────────────────────────────────────────────────
  // Paginated period transactions for the recent-tx tile. Budgets and the
  // forecast plan are loaded by their own sibling loaders, so this loader is
  // page-data only. Gated on realPeriodStart; stale-request guard matches
  // sibling loaders.
  const loadPageTransactions = useCallback(
    async (p: number) => {
      if (!realPeriodStart) {
        txPageRequestId.current += 1;
        setTransactions([]);
        setTxTotal(0);
        return;
      }
      const myId = ++txPageRequestId.current;
      try {
        const data = await apiFetch<{ items: Transaction[]; total: number }>(
          `/api/v1/transactions?limit=${pageSize}&offset=${p * pageSize}${txQueryTail}`,
        );
        if (txPageRequestId.current !== myId) return;
        setTransactions(data?.items ?? []);
        setTxTotal(data?.total ?? 0);
      } catch {
        if (txPageRequestId.current !== myId) return;
        // Silent — keep last good page on transient failures.
      }
    },
    [realPeriodStart, pageSize, txQueryTail],
  );

  // ── loadBudgets ─────────────────────────────────────────────────────────────
  // Per-period budgets. When realPeriodStart is known, request that specific
  // period. Mirrors the budgetUrl fetch in LegacyDashboard.loadTransactions.
  // On a transient failure, keep the last good budgets (don't blank them).
  // Stale-request guard matches sibling loaders.
  const loadBudgets = useCallback(async () => {
    // TBD-197 — load-bearing, not an optimisation. With Budgets gated off the
    // route 404s, apiFetch throws, and the dashboard would render a deliberate
    // org setting as a FAILURE. The tiles fall back to their own existing
    // empty states instead.
    //
    // ⚠ Do NOT extend this skip to loadAccountMonthEndForecast: that endpoint
    // is an account-projection engine (credit-card cycles + loan
    // amortization) that merely lives under a /forecast URL prefix, and
    // AccountMonthEndForecast renders a bare "Loading…" forever on null data.
    if (budgetsDisabled) {
      budgetsRequestId.current += 1;
      setBudgets([]);
      return;
    }
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
  }, [realPeriodStart, budgetsDisabled]);

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
    // Self-heal like the SWR error channels: a later successful load (e.g. a
    // post-write refresh) clears a stale mount-time error.
    setAuxError(null);
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
    // TBD-197 — load-bearing, not an optimisation, and this is the loader that
    // makes it so. With Forecast gated off `/api/v1/forecast` 404s, `apiFetch`
    // throws, and the catch below sets `projectionFailed = true`, which
    // `OnTrackTile` renders as an ERROR WITH A RETRY BUTTON. A deliberate org
    // setting must never render as a failure. Exiting through the same shape
    // as the `!realPeriodStart` guard leaves the tile in its EMPTY state.
    if (forecastDisabled) {
      projectionRequestId.current += 1;
      setForecastProjection(null);
      setProjectionFailed(false);
      setProjectionLoading(false);
      return;
    }
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
  }, [realPeriodStart, forecastDisabled]);

  // ── loadSpendingRollup (the donut's data path, TBD-221) ─────────────────────
  const loadSpendingRollup = useCallback(async () => {
    // ⚠⚠ NO `forecastDisabled` GUARD HERE, AND THAT IS THE POINT OF THE TICKET.
    // `/api/v1/transactions/spending-by-category` is a HISTORICAL ACTUALS
    // rollup on the transactions router; it is ungated server-side and answers
    // 200 for an org with Forecast switched off. Adding the guard would
    // reproduce the exact defect this endpoint was created to remove: "No
    // expense data yet" rendered over a period holding real settled expense.
    // The sibling `loadForecastProjection` above DOES carry the guard, because
    // its endpoint genuinely 404s — do not "make them consistent".
    if (!realPeriodStart) {
      rollupRequestId.current += 1;
      setSpendingRollup(null);
      setRollupFailed(false);
      setRollupLoading(false);
      return;
    }
    const myId = ++rollupRequestId.current;
    setSpendingRollup(null);
    setRollupFailed(false);
    setRollupLoading(true);
    try {
      // `period_start` is a HINT — the server may substitute the current
      // period. Nothing below reads `realPeriodStart` again; the window comes
      // back off the response (`rollupFrom` / `rollupTo`).
      const data = await apiFetch<SpendingByCategoryResponse>(
        `/api/v1/transactions/spending-by-category?period_start=${realPeriodStart}`,
      );
      if (rollupRequestId.current !== myId) return;
      setSpendingRollup(data);
      setRollupFailed(false);
    } catch {
      if (rollupRequestId.current !== myId) return;
      setSpendingRollup(null);
      setRollupFailed(true);
    } finally {
      if (rollupRequestId.current === myId) {
        setRollupLoading(false);
      }
    }
  }, [realPeriodStart]);

  // ── loadAccountMonthEndForecast ─────────────────────────────────────────────
  const loadAccountMonthEndForecast = useCallback(async () => {
    // ⚠⚠ NO `forecastDisabled` GUARD HERE, AND THAT IS DELIBERATE (TBD-197).
    // `/api/v1/forecast/account-balances` is an ACCOUNT-projection engine
    // (credit-card statement cycles + loan amortization) that merely shares the
    // /forecast URL prefix; it is ungated server-side, and `LoanPayoffTile` and
    // `CreditUtilizationWidget` — Credit-Card and Loan surfaces — read its
    // output. Adding the guard here also produces a PERMANENT FALSE LOADING
    // STATE, because `AccountMonthEndForecast` renders a bare "Loading…"
    // forever on null data rather than an empty state. Fenced by F12's
    // positive clause and by the backend's F7.
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
    // TBD-197 — `/api/v1/forecast-plans/current` 404s for a disabled org.
    // `null` is already this loader's "no plan yet" value, so the tiles fall
    // back to their own existing empty states.
    if (forecastDisabled) {
      forecastPlanRequestId.current += 1;
      setForecast(null);
      return;
    }
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
  }, [realPeriodStart, forecastDisabled]);

  // ── Initial load ────────────────────────────────────────────────────────────
  // Accounts + billing periods auto-fetch via the SWR hooks once refsEnabled
  // flips true; only the imperative loads remain here, gated the same way.
  useEffect(() => {
    if (!refsEnabled) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads auxiliary refs, surfacing failure into error state and settling the loaded flag
    loadAux()
      .catch((err: unknown) => {
        const msg =
          err instanceof Error ? err.message : "Failed to load dashboard data";
        setAuxError(msg);
      })
      .finally(() => setAuxSettled(true));
    void loadPendingTransactions();
  }, [refsEnabled, loadAux, loadPendingTransactions]);

  // Arm the stalled-refs fallback only while we are actually waiting (same
  // 10s bound as the transactions page, #520). Covers all three loading legs
  // (accounts / billing periods / settings aux): any of them stalling would
  // otherwise strand the skeleton forever.
  useEffect(() => {
    if (!refsEnabled || refsSettled) return;
    const timer = setTimeout(() => setRefsWaitElapsed(true), 10000);
    return () => clearTimeout(timer);
  }, [refsEnabled, refsSettled]);

  // ── Period-scoped loads (fire when realPeriodStart is known) ────────────────
  useEffect(() => {
    if (realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the forecast projection fetch that loads its result into state
      void loadForecastProjection();
    }
  }, [realPeriodStart, loadForecastProjection]);

  // The donut's rollup. Its own effect, not a line inside the projection's:
  // the two must be able to fail independently.
  useEffect(() => {
    if (realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the spending-by-category fetch that loads its result into state
      void loadSpendingRollup();
    }
  }, [realPeriodStart, loadSpendingRollup]);

  // FIX 4: gate account-forecast fetch on realPeriodStart being resolved,
  // matching the guard pattern on the sibling loadForecastProjection effect.
  useEffect(() => {
    if (realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the per-account month-end forecast fetch that loads its result into state
      void loadAccountMonthEndForecast();
    }
  }, [realPeriodStart, loadAccountMonthEndForecast]);

  useEffect(() => {
    if (realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the forecast-plan fetch that loads its result into state
      void loadForecastPlan();
    }
  }, [realPeriodStart, loadForecastPlan]);

  // Budgets fire once the periods request has SETTLED (or the stall fallback
  // elapsed) rather than on a bare mount fetch + a period-scoped refetch: the
  // pre-SWR shape issued the request twice on every cold mount (once without
  // period_start, then again with it). loadBudgets already falls back to the
  // bare URL (current-period default) when realPeriodStart is still null, so
  // an errored periods request degrades to the legacy behavior instead of
  // never loading budgets at all.
  useEffect(() => {
    if (periodsResolved) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the budgets fetch that loads its result into state
      void loadBudgets();
    }
  }, [periodsResolved, loadBudgets]);

  // Phase 2c: the paginated recent-tx page re-fetches when the period OR the
  // page changes. Period nav does NOT reset the page (mirrors LegacyDashboard).
  useEffect(() => {
    if (realPeriodStart) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- triggers the paginated recent-transactions fetch that loads its result into state
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
      loadSpendingRollup(),
      loadPendingTransactions(),
      loadAccountMonthEndForecast(),
      loadForecastPlan(),
      loadBudgets(),
      loadPageTransactions(0),
    ]);
  }, [
    mutateAccounts,
    mutateBillingPeriods,
    loadAux,
    loadForecastProjection,
    loadSpendingRollup,
    loadPendingTransactions,
    loadAccountMonthEndForecast,
    loadForecastPlan,
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

  // ── Spending by category: the SERVER rollup (TBD-221) ───────────────────────
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
  // Both payloads carry a per-category rollup of the same numbers, so reading
  // the forecast's copy compiles, type-checks and looks right — and blanks the
  // tile for every org that has Forecast switched off.
  //
  // ⚠ NO CLIENT FALLBACK. When the rollup is absent this is empty and the tile
  // renders its `rollupFailed` state. Re-aggregating here instead would
  // silently substitute the wrong number, which IS the defect being deleted.
  // `is_manual_adjustment` is on the wire and `reconciliation_state` is not, so
  // any client reconstruction can only ever be half of the filter.
  const donutDataRaw = useMemo<DonutDatum[]>(() => {
    const rows = spendingRollup?.categories;
    if (!Array.isArray(rows)) return [];
    return rows
      .map((r) => ({
        categoryId: r.category_id,
        name: r.category_name,
        value: Number(r.executed),
      }))
      .filter((d) => Number.isFinite(d.value) && d.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [spendingRollup]);

  // The sum of the RENDERED slices, which is the figure the percentages below
  // are taken against — so they add to 100 by construction. Server-side
  // `executed_expense` is itself the sum of the same rows, so the two agree
  // for every payload whose rows are all positive; the reduce is what keeps
  // them agreeing when a category nets to zero or below and drops out of the
  // slice list. What must NEVER back this figure is a second source — a
  // client re-aggregation, or `forecastProjection.executed_expense`, which is
  // null for a forecast-off org.
  const totalSpend = useMemo(
    () => donutDataRaw.reduce((s, d) => s + d.value, 0),
    [donutDataRaw],
  );

  const sortedSpending = useMemo<SortedSpendingRow[]>(() => {
    const list = donutDataRaw.map((d, i) => ({
      categoryId: d.categoryId,
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

  // All budgets feed the "Budget Progress" bar chart. The canvas tile flex-
  // fills its resizable box, so every category is shown (thinner bars as the
  // list grows); the user resizes the tile taller for thickness.
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

  // All expense items feed the "Forecast by Category" bar chart. Like the
  // budget tile, the canvas tile flex-fills its box so every category shows.
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

  // ── Chart-filter label (TBD-221) ────────────────────────────────────────────
  // The badge reads its name off the rollup rows already in memory, so the
  // label and the slice it describes have ONE source of truth. Budget and
  // Forecast bars can filter a category with no settled spend this period and
  // therefore no rollup row, so those two in-memory lists back the LABEL up;
  // the NUMBER always comes from the server.
  //
  // Two same-named subcategories now render as two slices sharing one label —
  // deliberately TBD-326, split out so a label question cannot gate a
  // correctness fix. A correct number under an ambiguous label beats a wrong
  // number under a unique one.
  const chartFilterName = useMemo(() => {
    if (chartFilter === null) return null;
    return (
      donutDataRaw.find((d) => d.categoryId === chartFilter)?.name ??
      dashBudgets.find((b) => b.category_id === chartFilter)?.category_name ??
      forecastExpenseItems.find((it) => it.category_id === chartFilter)
        ?.category_name ??
      null
    );
  }, [chartFilter, donutDataRaw, dashBudgets, forecastExpenseItems]);

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

  const { field: dashSortField, dir: dashSortDir, setSort: setDashSort } = dashSort;

  // The rendered list is the page the server returned — filtered or not.
  //
  // TBD-221 deleted the `tx.category_name === chartFilter` predicate that
  // used to live here: it compared on a name (not the rollup's identity) over
  // a capped snapshot (so it could not see rows past 200), which is how the
  // list could disagree with the slice that opened it. The server filters now.
  //
  // ⚠ Copy before sorting. `transactions` is state and `Array.prototype.sort`
  // mutates in place; the deleted `.filter()` used to hand `.sort()` a fresh
  // array, so dropping it without the spread would sort React state under the
  // reducer. NO client-side dedupe either: the unfiltered page passes
  // collapse_transfers=true and the filtered one passes reportable=true, so
  // the server folded/dropped transfer legs BEFORE the limit (TBD-268).
  const sortedVisibleTxs = useMemo(
    () =>
      [...transactions]
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
    [transactions, dashSortField, dashSortDir],
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
  // + refs awaited; on page 0 the budgets/forecast plan refresh too
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
        void loadBudgets();
        void loadForecastPlan();
      }
      await Promise.all([mutateAccounts(), mutateBillingPeriods(), loadAux()]);
      void loadForecastProjection();
      // A settled/pending toggle moves the row in and out of the donut's
      // SETTLED bucket, so the rollup refreshes alongside the projection —
      // and independently of `page`, since the donut is not the tx page.
      void loadSpendingRollup();
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
      loadBudgets,
      loadForecastPlan,
      loadForecastProjection,
      loadSpendingRollup,
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
    rollupFailed,
    rollupLoading,
    onRetryRollup: loadSpendingRollup,
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
    chartFilterName,
    setChartFilter,
    // Phase 2c recent transactions
    transactions,
    txTotal,
    page,
    setPage,
    pageSize,
    setPageSize,
    sortedVisibleTxs,
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
