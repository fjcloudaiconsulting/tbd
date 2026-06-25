/**
 * Tests for DashboardDataProvider + useDashboard().
 *
 * Mock strategy: vi.mock("@/lib/api") intercepts apiFetch at the module
 * boundary (same pattern as dashboard-refresh-error-banner.test.tsx and
 * dashboard-projection-race.test.tsx).
 * Mock strategy for fetchAll: vi.mock("@/lib/pagination") — fetchAll calls
 * apiFetch internally, but since pagination.ts imports apiFetch at module
 * level we mock the whole module so fetchAll returns a controllable value.
 * Mock strategy for usePersistedSort: vi.mock("@/lib/hooks/use-persisted-sort")
 * — mocked to avoid localStorage side-effects and to allow sort-toggle
 * assertions without full hook wiring.
 */
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";

import { DashboardDataProvider, useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { apiFetch } from "@/lib/api";
import * as pagination from "@/lib/pagination";
import * as usePersistedSortModule from "@/lib/hooks/use-persisted-sort";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn() };
});

// FIX 5: DashboardDataProvider now calls useAuth() to seed billingCycleDay.
vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(() => ({ user: { billing_cycle_day: 1 }, loading: false })),
  };
});

// Mock usePersistedSort to avoid localStorage side-effects. Returns a
// controllable state object; tests that need to verify sort toggling
// inspect the mock call count / args.
vi.mock("@/lib/hooks/use-persisted-sort", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/hooks/use-persisted-sort")
  >("@/lib/hooks/use-persisted-sort");
  return {
    ...actual,
    usePersistedSort: vi.fn(() => ({
      field: "amount",
      dir: "desc",
      setSort: vi.fn(),
      reset: vi.fn(),
      isDefault: true,
    })),
  };
});

// ── fixtures ──────────────────────────────────────────────────────────────────

const PAST_PERIOD = { id: 1, start_date: "2026-04-01", end_date: "2026-04-30" };
const CURRENT_PERIOD = { id: 2, start_date: "2026-05-01", end_date: null };
const FUTURE_PERIOD = { id: 3, start_date: "2099-07-01", end_date: "2099-07-31" };

const PERIODS = [CURRENT_PERIOD, PAST_PERIOD];

// Phase 2b fixtures
const TX_EXPENSE = {
  id: 1,
  account_id: 1,
  account_name: "Checking",
  category_id: 10,
  category_name: "Groceries",
  description: "Supermarket",
  amount: 50,
  type: "expense" as const,
  status: "settled" as const,
  linked_transaction_id: null,
  recurring_id: null,
  date: "2026-05-10",
  settled_date: null,
  is_imported: false,
  is_manual_adjustment: false,
  tags: [],
};

const TX_TRANSFER = {
  ...TX_EXPENSE,
  id: 2,
  category_name: "Transfer",
  linked_transaction_id: 1, // transfer half — should be excluded from donut
};

const TX_INCOME = {
  ...TX_EXPENSE,
  id: 3,
  category_name: "Salary",
  type: "income" as const,
};

const BUDGET_1 = {
  id: 1,
  category_id: 10,
  category_name: "Groceries",
  amount: 200,
  spent: 50,
  remaining: 150,
  percent_used: 25,
  period_start: "2026-05-01",
  period_end: "2026-05-31",
};

const FORECAST_PLAN_WITH_ITEMS = {
  id: 1,
  billing_period_id: 2,
  period_start: "2026-05-01",
  period_end: null,
  status: "active" as const,
  total_planned_income: "3000",
  total_planned_expense: "2000",
  total_actual_income: "3000",
  total_actual_expense: "1500",
  items: [
    {
      id: 1,
      plan_id: 1,
      category_id: 10,
      category_name: "Groceries",
      parent_id: null,
      type: "expense" as const,
      planned_amount: "200",
      source: "manual" as const,
      actual_amount: "50",
      variance: "150",
    },
    {
      id: 2,
      plan_id: 1,
      category_id: 20,
      category_name: "Salary",
      parent_id: null,
      type: "income" as const,
      planned_amount: "3000",
      source: "manual" as const,
      actual_amount: "3000",
      variance: "0",
    },
  ],
};

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const INACTIVE_ACCT = { ...ACCT, id: 2, name: "Closed", is_active: false };

const FORECAST_PROJECTION = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  executed_income: "0",
  executed_expense: "100",
  executed_net: "-100",
  pending_income: "0",
  pending_expense: "0",
  recurring_income: "0",
  recurring_expense: "0",
  forecast_income: "0",
  forecast_expense: "500",
  forecast_net: "-500",
  categories: [],
};

const ACCOUNT_MONTH_END = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [],
  accounts: [],
};

// ── helpers ───────────────────────────────────────────────────────────────────

function makeApiFetchHandler(overrides: Record<string, unknown> = {}) {
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return overrides.accounts ?? [ACCT];
    if (url.startsWith("/api/v1/settings/billing-periods"))
      return overrides.periods ?? PERIODS;
    if (url.startsWith("/api/v1/settings/billing-period"))
      return overrides.period ?? CURRENT_PERIOD;
    if (url.startsWith("/api/v1/settings/billing-cycle"))
      return overrides.billingCycle ?? { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/forecast-plans/current"))
      return overrides.forecastPlan ?? null;
    if (url.startsWith("/api/v1/forecast/account-balances"))
      return overrides.accountMonthEnd ?? ACCOUNT_MONTH_END;
    if (url.startsWith("/api/v1/forecast"))
      return overrides.projection ?? FORECAST_PROJECTION;
    // Phase 2b: transactions snapshot + budgets
    if (url.startsWith("/api/v1/transactions"))
      return overrides.transactions ?? { items: [], total: 0 };
    if (url.startsWith("/api/v1/budgets"))
      return overrides.budgets ?? [];
    return null;
  };
}

/**
 * A consumer component that renders the useDashboard() context values as
 * data-testid attributes so tests can assert on them without coupling to
 * display copy.
 */
function Consumer() {
  const ctx = useDashboard();
  return (
    <div>
      <span data-testid="accounts-count">{ctx.accounts.length}</span>
      <span data-testid="active-accounts-count">{ctx.activeAccounts.length}</span>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="error">{ctx.error ?? ""}</span>
      <span data-testid="period-idx">{ctx.periodIdx}</span>
      <span data-testid="month-from">{ctx.monthFrom}</span>
      <span data-testid="month-to">{ctx.monthTo}</span>
      <span data-testid="is-current">{String(ctx.isCurrentSelectedPeriod)}</span>
      <span data-testid="is-past">{String(ctx.isPastSelectedPeriod)}</span>
      <span data-testid="is-future">{String(ctx.isFutureSelectedPeriod)}</span>
      <span data-testid="projection-failed">{String(ctx.projectionFailed)}</span>
      <span data-testid="projection-loading">{String(ctx.projectionLoading)}</span>
      <span data-testid="has-projection">{String(ctx.forecastProjection !== null)}</span>
      <span data-testid="has-account-forecast">{String(ctx.accountMonthEndForecast !== null)}</span>
      <span data-testid="account-forecast-error">{String(ctx.accountMonthEndForecastError)}</span>
      <span data-testid="pending-acct-1">{ctx.pendingByAccount[1] ?? 0}</span>
      <button data-testid="retry-projection" onClick={ctx.onRetryProjection} />
      <button data-testid="jump-to-current" onClick={ctx.jumpToCurrentPeriod} />
      <button data-testid="refresh" onClick={() => void ctx.refresh()} />
    </div>
  );
}

function renderProvider() {
  return render(
    <DashboardDataProvider>
      <Consumer />
    </DashboardDataProvider>,
  );
}

// ── tests ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(pagination.fetchAll).mockReset();
  // Default: no pending transactions.
  vi.mocked(pagination.fetchAll).mockResolvedValue([]);
  // Reset the usePersistedSort mock to its default state.
  vi.mocked(usePersistedSortModule.usePersistedSort).mockReturnValue({
    field: "amount",
    dir: "desc",
    setSort: vi.fn(),
    reset: vi.fn(),
    isDefault: true,
  });
});

describe("DashboardDataProvider — initial fetch", () => {
  it("fetches refs, projection, account forecast, snapshot and budgets on mount", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderProvider();

    await waitFor(() => {
      expect(screen.getByTestId("loading").textContent).toBe("false");
    });

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.startsWith("/api/v1/accounts"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/settings/billing-periods"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/settings/billing-cycle"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/forecast?period_start="))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/forecast/account-balances"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/forecast-plans/current"))).toBe(true);
    // Phase 2b: snapshot + budgets
    expect(calls.some((u) => u.startsWith("/api/v1/transactions?limit=200"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/v1/budgets?period_start="))).toBe(true);
  });

  it("exposes accounts and activeAccounts (filters inactive)", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ accounts: [ACCT, INACTIVE_ACCT] }) as never,
    );

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    expect(screen.getByTestId("accounts-count").textContent).toBe("2");
    expect(screen.getByTestId("active-accounts-count").textContent).toBe("1");
  });
});

describe("DashboardDataProvider — period derivations", () => {
  it("defaults to the current period (end_date === null)", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    expect(screen.getByTestId("is-current").textContent).toBe("true");
    expect(screen.getByTestId("is-past").textContent).toBe("false");
    expect(screen.getByTestId("is-future").textContent).toBe("false");
    expect(screen.getByTestId("month-from").textContent).toBe("2026-05-01");
  });

  it("isPastSelectedPeriod is true when end_date is before today", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ periods: [PAST_PERIOD], period: PAST_PERIOD }) as never,
    );

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    expect(screen.getByTestId("is-past").textContent).toBe("true");
    expect(screen.getByTestId("is-current").textContent).toBe("false");
    expect(screen.getByTestId("is-future").textContent).toBe("false");
  });

  it("isFutureSelectedPeriod is true when start_date is after today", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({
        periods: [FUTURE_PERIOD, CURRENT_PERIOD],
        period: CURRENT_PERIOD,
      }) as never,
    );

    // Use a consumer that also exposes setPeriodIdx so we can navigate to
    // the future period (index 0 in [FUTURE_PERIOD, CURRENT_PERIOD]).
    function ConsumerWithSetIdx() {
      const ctx = useDashboard();
      return (
        <div>
          <span data-testid="loading">{String(ctx.loading)}</span>
          <span data-testid="is-current">{String(ctx.isCurrentSelectedPeriod)}</span>
          <span data-testid="is-future">{String(ctx.isFutureSelectedPeriod)}</span>
          <button data-testid="go-to-0" onClick={() => ctx.setPeriodIdx(0)} />
        </div>
      );
    }

    render(
      <DashboardDataProvider>
        <ConsumerWithSetIdx />
      </DashboardDataProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Provider defaults to current period (end_date === null), which is
    // index 1 in [FUTURE_PERIOD, CURRENT_PERIOD].
    expect(screen.getByTestId("is-current").textContent).toBe("true");
    expect(screen.getByTestId("is-future").textContent).toBe("false");

    // Navigate to periodIdx 0 (the future period).
    act(() => {
      screen.getByTestId("go-to-0").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("is-future").textContent).toBe("true"),
    );
    expect(screen.getByTestId("is-current").textContent).toBe("false");
  });
});

describe("DashboardDataProvider — pendingByAccount", () => {
  it("computes pendingByAccount from pending transactions", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);
    vi.mocked(pagination.fetchAll).mockResolvedValue([
      {
        id: 10,
        account_id: 1,
        type: "expense",
        amount: "50",
        status: "pending",
      },
      {
        id: 11,
        account_id: 1,
        type: "income",
        amount: "20",
        status: "pending",
      },
    ] as never);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // expense sign = -1: -50; income sign = +1: +20 → net = -30
    expect(screen.getByTestId("pending-acct-1").textContent).toBe("-30");
  });
});

describe("DashboardDataProvider — projection failure + retry", () => {
  it("sets projectionFailed when projection fetch throws", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url.startsWith("/api/v1/accounts")) return Promise.resolve([ACCT]);
      if (url.startsWith("/api/v1/settings/billing-periods")) return Promise.resolve(PERIODS);
      if (url.startsWith("/api/v1/settings/billing-period")) return Promise.resolve(CURRENT_PERIOD);
      if (url.startsWith("/api/v1/settings/billing-cycle")) return Promise.resolve({ billing_cycle_day: 1 });
      if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast/account-balances")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast")) return Promise.reject(new Error("backend 500"));
      return Promise.resolve(null);
    }) as never);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("projection-failed").textContent).toBe("true"),
    );
    expect(screen.getByTestId("has-projection").textContent).toBe("false");
  });

  it("onRetryProjection re-fetches the projection", async () => {
    let projectionCalls = 0;
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url.startsWith("/api/v1/accounts")) return Promise.resolve([ACCT]);
      if (url.startsWith("/api/v1/settings/billing-periods")) return Promise.resolve(PERIODS);
      if (url.startsWith("/api/v1/settings/billing-period")) return Promise.resolve(CURRENT_PERIOD);
      if (url.startsWith("/api/v1/settings/billing-cycle")) return Promise.resolve({ billing_cycle_day: 1 });
      if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast/account-balances")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast")) {
        projectionCalls += 1;
        if (projectionCalls === 1) return Promise.reject(new Error("first attempt fails"));
        return Promise.resolve(FORECAST_PROJECTION);
      }
      return Promise.resolve(null);
    }) as never);

    renderProvider();

    // Wait for initial projection failure.
    await waitFor(() =>
      expect(screen.getByTestId("projection-failed").textContent).toBe("true"),
    );

    // Retry.
    act(() => {
      screen.getByTestId("retry-projection").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("has-projection").textContent).toBe("true"),
    );
    expect(screen.getByTestId("projection-failed").textContent).toBe("false");
    expect(projectionCalls).toBe(2);
  });
});

describe("DashboardDataProvider — jumpToCurrentPeriod", () => {
  it("sets periodIdx to the period with end_date === null", async () => {
    // Periods with past at index 0 and current at index 1.
    const periodsOldestFirst = [PAST_PERIOD, CURRENT_PERIOD];
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ periods: periodsOldestFirst, period: PAST_PERIOD }) as never,
    );

    /**
     * Component that also exposes a button to set periodIdx to 0 (the past
     * period) so we can then jump back to current.
     */
    function ConsumerWithSetIdx() {
      const ctx = useDashboard();
      return (
        <div>
          <span data-testid="period-idx">{ctx.periodIdx}</span>
          <span data-testid="is-current">{String(ctx.isCurrentSelectedPeriod)}</span>
          <button data-testid="go-to-0" onClick={() => ctx.setPeriodIdx(0)} />
          <button data-testid="jump-to-current" onClick={ctx.jumpToCurrentPeriod} />
        </div>
      );
    }

    render(
      <DashboardDataProvider>
        <ConsumerWithSetIdx />
      </DashboardDataProvider>,
    );

    await waitFor(() =>
      // After loadRefs, provider jumps to current period (index 1).
      expect(screen.getByTestId("is-current").textContent).toBe("true"),
    );

    // Manually navigate to period 0 (past).
    act(() => {
      screen.getByTestId("go-to-0").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("is-current").textContent).toBe("false"),
    );

    // Jump back to current.
    act(() => {
      screen.getByTestId("jump-to-current").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("is-current").textContent).toBe("true"),
    );
  });
});

describe("DashboardDataProvider — pfv:transaction-added event", () => {
  it("re-fetches data when pfv:transaction-added is dispatched", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    const accountCallsBefore = vi
      .mocked(apiFetch)
      .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/accounts")).length;

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    await waitFor(() => {
      const accountCallsAfter = vi
        .mocked(apiFetch)
        .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/accounts")).length;
      expect(accountCallsAfter).toBeGreaterThan(accountCallsBefore);
    });
  });
});

describe("useDashboard — throws outside provider", () => {
  it("throws when used outside DashboardDataProvider", () => {
    const err = console.error;
    console.error = () => {};
    expect(() => render(<Consumer />)).toThrow(
      /useDashboard must be used within a DashboardDataProvider/,
    );
    console.error = err;
  });
});

// ── Phase 2b: chart data tests ────────────────────────────────────────────────

/**
 * Extended consumer that exposes Phase 2b context values as data-testid spans.
 */
function ConsumerChart() {
  const ctx = useDashboard();
  return (
    <div>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="all-tx-count">{ctx.allTransactions.length}</span>
      <span data-testid="budgets-count">{ctx.budgets.length}</span>
      <span data-testid="dash-budgets-count">{ctx.dashBudgets.length}</span>
      <span data-testid="donut-data-count">{ctx.donutData.length}</span>
      <span data-testid="total-spend">{ctx.totalSpend}</span>
      <span data-testid="sorted-spending-count">{ctx.sortedSpending.length}</span>
      <span data-testid="budget-chart-data-count">{ctx.budgetChartData.length}</span>
      <span data-testid="forecast-expense-items-count">{ctx.forecastExpenseItems.length}</span>
      <span data-testid="forecast-chart-rows-count">{ctx.forecastChartRows.length}</span>
      <span data-testid="chart-filter">{ctx.chartFilter ?? "null"}</span>
      <span data-testid="spending-sort-field">{ctx.spendingSort.field}</span>
      <button
        data-testid="set-chart-filter"
        onClick={() => ctx.setChartFilter("Groceries")}
      />
      <button
        data-testid="clear-chart-filter"
        onClick={() => ctx.setChartFilter(null)}
      />
      <button
        data-testid="toggle-spending-sort"
        onClick={() => ctx.toggleSpendingSort("name")}
      />
      <button
        data-testid="set-period-idx"
        onClick={() => ctx.setPeriodIdx(1)}
      />
      <button
        data-testid="jump-to-current"
        onClick={ctx.jumpToCurrentPeriod}
      />
      <button data-testid="refresh" onClick={() => void ctx.refresh()} />
    </div>
  );
}

function renderChartProvider() {
  return render(
    <DashboardDataProvider>
      <ConsumerChart />
    </DashboardDataProvider>,
  );
}

describe("DashboardDataProvider — Phase 2b: snapshot + budgets fetch", () => {
  it("fetches the period snapshot once realPeriodStart resolves", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({
        transactions: { items: [TX_EXPENSE], total: 1 },
      }) as never,
    );

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Wait for period-scoped loads to complete.
    await waitFor(() =>
      expect(screen.getByTestId("all-tx-count").textContent).toBe("1"),
    );

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.startsWith("/api/v1/transactions?limit=200"))).toBe(true);
  });

  it("fetches budgets once realPeriodStart resolves", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ budgets: [BUDGET_1] }) as never,
    );

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    await waitFor(() =>
      expect(screen.getByTestId("budgets-count").textContent).toBe("1"),
    );

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => c[0] as string);
    expect(calls.some((u) => u.startsWith("/api/v1/budgets?period_start="))).toBe(true);
  });

  it("refresh() re-fetches snapshot and budgets", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);
    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    const txCallsBefore = vi
      .mocked(apiFetch)
      .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/transactions?limit=200")).length;

    const budgetCallsBefore = vi
      .mocked(apiFetch)
      .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/budgets?period_start=")).length;

    act(() => {
      screen.getByTestId("refresh").click();
    });

    await waitFor(() => {
      const txCallsAfter = vi
        .mocked(apiFetch)
        .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/transactions?limit=200")).length;
      expect(txCallsAfter).toBeGreaterThan(txCallsBefore);
    });

    const budgetCallsAfter = vi
      .mocked(apiFetch)
      .mock.calls.filter((c) => (c[0] as string).startsWith("/api/v1/budgets?period_start=")).length;
    expect(budgetCallsAfter).toBeGreaterThan(budgetCallsBefore);
  });
});

describe("DashboardDataProvider — Phase 2b: donut/spending memos", () => {
  it("donutData excludes transfer legs and income; totalSpend sums expenses", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({
        transactions: {
          items: [TX_EXPENSE, TX_TRANSFER, TX_INCOME],
          total: 3,
        },
      }) as never,
    );

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Only TX_EXPENSE qualifies (settled expense, no linked_transaction_id)
    await waitFor(() =>
      expect(screen.getByTestId("donut-data-count").textContent).toBe("1"),
    );
    expect(screen.getByTestId("total-spend").textContent).toBe("50");
    expect(screen.getByTestId("sorted-spending-count").textContent).toBe("1");
  });

  it("donutData is empty when allTransactions is empty", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    await waitFor(() =>
      expect(screen.getByTestId("donut-data-count").textContent).toBe("0"),
    );
    expect(screen.getByTestId("total-spend").textContent).toBe("0");
  });
});

describe("DashboardDataProvider — Phase 2b: budgetChartData memo", () => {
  it("budgetChartData maps first 6 budgets correctly", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ budgets: [BUDGET_1] }) as never,
    );

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    await waitFor(() =>
      expect(screen.getByTestId("budget-chart-data-count").textContent).toBe("1"),
    );
    expect(screen.getByTestId("dash-budgets-count").textContent).toBe("1");
  });
});

describe("DashboardDataProvider — Phase 2b: forecastChartRows memo", () => {
  it("forecastChartRows contains only expense items (up to 8)", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({ forecastPlan: FORECAST_PLAN_WITH_ITEMS }) as never,
    );

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // FORECAST_PLAN_WITH_ITEMS has 1 expense + 1 income → 1 expense item
    await waitFor(() =>
      expect(screen.getByTestId("forecast-expense-items-count").textContent).toBe("1"),
    );
    expect(screen.getByTestId("forecast-chart-rows-count").textContent).toBe("1");
  });

  it("forecastChartRows is empty when forecast is null", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    await waitFor(() =>
      expect(screen.getByTestId("forecast-expense-items-count").textContent).toBe("0"),
    );
    expect(screen.getByTestId("forecast-chart-rows-count").textContent).toBe("0");
  });
});

describe("DashboardDataProvider — Phase 2b: toggleSpendingSort", () => {
  it("toggleSpendingSort calls setSort with new field + default dir on field change", () => {
    const mockSetSort = vi.fn();
    vi.mocked(usePersistedSortModule.usePersistedSort).mockReturnValue({
      field: "amount",
      dir: "desc",
      setSort: mockSetSort,
      reset: vi.fn(),
      isDefault: true,
    });

    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    // toggleSpendingSort("name") — different field → setSort("name", "asc")
    act(() => {
      screen.getByTestId("toggle-spending-sort").click();
    });

    expect(mockSetSort).toHaveBeenCalledWith("name", "asc");
  });

  it("toggleSpendingSort flips direction when same field", () => {
    const mockSetSort = vi.fn();
    vi.mocked(usePersistedSortModule.usePersistedSort).mockReturnValue({
      field: "name",
      dir: "asc",
      setSort: mockSetSort,
      reset: vi.fn(),
      isDefault: false,
    });

    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    // toggleSpendingSort("name") with field already "name" + dir "asc" → setSort("name", "desc")
    act(() => {
      screen.getByTestId("toggle-spending-sort").click();
    });

    expect(mockSetSort).toHaveBeenCalledWith("name", "desc");
  });
});

describe("DashboardDataProvider — Phase 2b: chartFilter", () => {
  it("setChartFilter sets the filter value", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    expect(screen.getByTestId("chart-filter").textContent).toBe("null");

    act(() => {
      screen.getByTestId("set-chart-filter").click();
    });

    expect(screen.getByTestId("chart-filter").textContent).toBe("Groceries");
  });

  it("setChartFilter(null) clears the filter", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    act(() => {
      screen.getByTestId("set-chart-filter").click();
    });
    expect(screen.getByTestId("chart-filter").textContent).toBe("Groceries");

    act(() => {
      screen.getByTestId("clear-chart-filter").click();
    });
    expect(screen.getByTestId("chart-filter").textContent).toBe("null");
  });

  it("setPeriodIdx clears chartFilter", async () => {
    vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);

    renderChartProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Set filter first
    act(() => {
      screen.getByTestId("set-chart-filter").click();
    });
    expect(screen.getByTestId("chart-filter").textContent).toBe("Groceries");

    // Navigate to period index 1 — should clear the filter
    act(() => {
      screen.getByTestId("set-period-idx").click();
    });

    expect(screen.getByTestId("chart-filter").textContent).toBe("null");
  });

  it("jumpToCurrentPeriod clears chartFilter", async () => {
    // Use periods with past at idx 0 and current at idx 1 so jumpToCurrentPeriod
    // actually changes the idx and the clear fires.
    vi.mocked(apiFetch).mockImplementation(
      makeApiFetchHandler({
        periods: [PAST_PERIOD, CURRENT_PERIOD],
        period: CURRENT_PERIOD,
      }) as never,
    );

    function ConsumerWithJump() {
      const ctx = useDashboard();
      return (
        <div>
          <span data-testid="loading">{String(ctx.loading)}</span>
          <span data-testid="chart-filter">{ctx.chartFilter ?? "null"}</span>
          <span data-testid="period-idx">{ctx.periodIdx}</span>
          <button
            data-testid="set-filter"
            onClick={() => ctx.setChartFilter("Groceries")}
          />
          <button
            data-testid="go-to-0"
            onClick={() => ctx.setPeriodIdx(0)}
          />
          <button
            data-testid="jump-to-current"
            onClick={ctx.jumpToCurrentPeriod}
          />
        </div>
      );
    }

    render(
      <DashboardDataProvider>
        <ConsumerWithJump />
      </DashboardDataProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Navigate away from current period
    act(() => {
      screen.getByTestId("go-to-0").click();
    });

    // Set filter while on past period
    act(() => {
      screen.getByTestId("set-filter").click();
    });
    expect(screen.getByTestId("chart-filter").textContent).toBe("Groceries");

    // jumpToCurrentPeriod should clear the filter
    act(() => {
      screen.getByTestId("jump-to-current").click();
    });

    expect(screen.getByTestId("chart-filter").textContent).toBe("null");
  });
});
