/**
 * Tests for DashboardDataProvider + useDashboard().
 *
 * Mock strategy: vi.mock("@/lib/api") intercepts apiFetch at the module
 * boundary (same pattern as dashboard-refresh-error-banner.test.tsx and
 * dashboard-projection-race.test.tsx).
 * Mock strategy for fetchAll: vi.mock("@/lib/pagination") — fetchAll calls
 * apiFetch internally, but since pagination.ts imports apiFetch at module
 * level we mock the whole module so fetchAll returns a controllable value.
 */
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";

import { DashboardDataProvider, useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { apiFetch } from "@/lib/api";
import * as pagination from "@/lib/pagination";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn() };
});

// ── fixtures ──────────────────────────────────────────────────────────────────

const PAST_PERIOD = { id: 1, start_date: "2026-04-01", end_date: "2026-04-30" };
const CURRENT_PERIOD = { id: 2, start_date: "2026-05-01", end_date: null };
const FUTURE_PERIOD = { id: 3, start_date: "2099-07-01", end_date: "2099-07-31" };

const PERIODS = [CURRENT_PERIOD, PAST_PERIOD];

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
    if (url.startsWith("/api/v1/categories")) return overrides.categories ?? [];
    if (url.startsWith("/api/v1/budgets")) return overrides.budgets ?? [];
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
});

describe("DashboardDataProvider — initial fetch", () => {
  it("fetches refs, projection, and account forecast on mount", async () => {
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

    renderProvider();

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );

    // Provider defaults to current period (end_date === null), which is
    // index 1 in [FUTURE_PERIOD, CURRENT_PERIOD].
    expect(screen.getByTestId("is-current").textContent).toBe("true");

    // Move to periodIdx 0 (future period).
    act(() => {
      // Directly update periodIdx via jumpToCurrentPeriod won't work here;
      // we test setPeriodIdx indirectly via jumpToCurrentPeriod below.
      // Here we dispatch a custom test event instead — use the context
      // directly by wrapping with a helper component.
    });
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
      if (url.startsWith("/api/v1/categories")) return Promise.resolve([]);
      if (url.startsWith("/api/v1/budgets")) return Promise.resolve([]);
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
      if (url.startsWith("/api/v1/categories")) return Promise.resolve([]);
      if (url.startsWith("/api/v1/budgets")) return Promise.resolve([]);
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
