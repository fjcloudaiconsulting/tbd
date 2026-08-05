/**
 * TBD-197 PR 1 — F12 (budgets clause).
 *
 * The fetch skip in DashboardDataProvider is load-bearing, not an
 * optimisation. With Budgets off the route 404s, `apiFetch` throws, and the
 * dashboard would render a *failure* for what is a deliberate org setting.
 *
 * The second half of the fence matters just as much: `/forecast/account-balances`
 * must STILL be fetched. It is an account-projection engine (credit-card
 * statement cycles + loan amortization) that merely lives under a /forecast URL
 * prefix, and `AccountMonthEndForecast.tsx` renders a bare "Loading…" forever
 * when its data is null — so over-gating it produces a permanent false loading
 * state, not an empty state.
 *
 * ⚠ The fixture MUST select the current period: `loadAccountMonthEndForecast`
 * early-returns on `!realPeriodStart || !isCurrentSelectedPeriod`, which would
 * make the positive clause falsely RED.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { renderWithSWR } from "@/tests/utils/render-with-swr";
import {
  DashboardDataProvider,
  useDashboard,
} from "@/components/dashboard/DashboardDataProvider";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import * as pagination from "@/lib/pagination";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>(
    "@/lib/pagination",
  );
  return { ...actual, fetchAll: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return { ...actual, useAuth: vi.fn() };
});

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

const CURRENT_PERIOD = { id: 2, start_date: "2026-05-01", end_date: null };

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

const ACCOUNT_MONTH_END = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [],
  accounts: [],
};

function handler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT];
    if (url.startsWith("/api/v1/settings/billing-periods")) return [CURRENT_PERIOD];
    if (url.startsWith("/api/v1/settings/billing-period")) return CURRENT_PERIOD;
    if (url.startsWith("/api/v1/settings/billing-cycle"))
      return { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/forecast-plans/current")) return null;
    if (url.startsWith("/api/v1/forecast/account-balances"))
      return ACCOUNT_MONTH_END;
    if (url.startsWith("/api/v1/forecast")) return null;
    if (url.startsWith("/api/v1/transactions")) return { items: [], total: 0 };
    if (url.startsWith("/api/v1/budgets")) return [];
    return null;
  };
}

function Consumer() {
  const ctx = useDashboard();
  return (
    <div>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="is-current">{String(ctx.isCurrentSelectedPeriod)}</span>
      <span data-testid="budgets-count">{ctx.budgets.length}</span>
      <span data-testid="has-account-forecast">
        {String(ctx.accountMonthEndForecast !== null)}
      </span>
    </div>
  );
}

function setAuth(features: Record<string, boolean> | undefined) {
  vi.mocked(useAuth).mockReturnValue({
    user: { billing_cycle_day: 1 },
    loading: false,
    features,
  } as never);
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(apiFetch).mockReset();
  vi.mocked(pagination.fetchAll).mockReset();
  vi.mocked(pagination.fetchAll).mockResolvedValue([]);
  vi.mocked(apiFetch).mockImplementation(handler() as never);
});

describe("DashboardDataProvider — budgets fetch skip (TBD-197 F12)", () => {
  it("never requests /api/v1/budgets when features.budgets is false, but still requests /forecast/account-balances", async () => {
    setAuth({
      reports: false,
      plans: false,
      customDashboard: false,
      forecast: true,
      budgets: false,
    });

    renderWithSWR(
      <DashboardDataProvider>
        <Consumer />
      </DashboardDataProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading").textContent).toBe("false"),
    );
    // The fixture must land on the current period or the positive clause below
    // is suppressed by loadAccountMonthEndForecast's own early return.
    await waitFor(() =>
      expect(screen.getByTestId("is-current").textContent).toBe("true"),
    );
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(
        calls.some((u) => u.startsWith("/api/v1/forecast/account-balances")),
      ).toBe(true);
    });

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(calls.filter((u) => u.startsWith("/api/v1/budgets"))).toEqual([]);
  });

  it("control: requests /api/v1/budgets when features.budgets is true", async () => {
    setAuth({
      reports: false,
      plans: false,
      customDashboard: false,
      forecast: true,
      budgets: true,
    });

    renderWithSWR(
      <DashboardDataProvider>
        <Consumer />
      </DashboardDataProvider>,
    );

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/budgets"))).toBe(true);
    });
  });

  it("control: an absent features object (booting client) still fetches budgets", async () => {
    setAuth(undefined);

    renderWithSWR(
      <DashboardDataProvider>
        <Consumer />
      </DashboardDataProvider>,
    );

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/budgets"))).toBe(true);
    });
  });
});
