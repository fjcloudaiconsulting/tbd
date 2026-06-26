import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner" as const,
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 100,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const PERIOD = {
  id: 1,
  start_date: "2026-05-01",
  end_date: null,
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.history.pushState({}, "", "/dashboard");
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
});

// Successful first-load handler that the test then mutates between
// initial render and the post-event refresh. Returns both the call
// counter and the response shape so the test can target individual
// endpoints.
function makeHandler(opts: { failTxOnSecondCall?: boolean } = {}) {
  let txCalls = 0;
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
    if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 } as never;
    if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
    if (url.startsWith("/api/v1/forecast/account-balances")) return null as never;
    if (url.startsWith("/api/v1/forecast")) {
      return {
        period_start: "2026-05-01",
        period_end: "2026-05-31",
        executed_income: "0",
        executed_expense: "0",
        executed_net: "0",
        pending_income: "0",
        pending_expense: "0",
        recurring_income: "0",
        recurring_expense: "0",
        forecast_income: "0",
        forecast_expense: "0",
        forecast_net: "0",
        categories: [],
      } as never;
    }
    if (url.startsWith("/api/v1/transactions")) {
      txCalls += 1;
      if (opts.failTxOnSecondCall && txCalls >= 2) {
        throw new Error("backend hiccup");
      }
      return { items: [], total: 0, limit: 200, offset: 0 } as never;
    }
    return null as never;
  };
}

describe("Dashboard refresh-error banner", () => {
  it("does not show the banner on a clean refresh", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler());

    render(<DashboardPage />);

    await waitFor(() => {
      // Initial load completes when transactions have been fetched.
      expect(
        vi.mocked(apiFetch).mock.calls.some(
          (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/transactions"),
        ),
      ).toBe(true);
    });

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    // Allow the post-event Promise.allSettled chain to settle.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.queryByTestId("dashboard-refresh-error")).toBeNull();
  });

  it("surfaces an inline retry affordance when a refresh call rejects, and other reloads still ran", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ failTxOnSecondCall: true }));

    render(<DashboardPage />);

    await waitFor(() => {
      const txCalls = vi
        .mocked(apiFetch)
        .mock.calls.filter(
          (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/transactions"),
        ).length;
      expect(txCalls).toBeGreaterThanOrEqual(1);
    });
    const acctsBefore = vi
      .mocked(apiFetch)
      .mock.calls.filter(
        (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/accounts"),
      ).length;

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    await waitFor(() => {
      expect(screen.getByTestId("dashboard-refresh-error")).toBeTruthy();
    });

    // The other reloads (refs, projection, etc.) still ran despite the
    // transactions reload throwing, proving Promise.allSettled is in
    // play rather than a single try/catch around all of them.
    const acctsAfter = vi
      .mocked(apiFetch)
      .mock.calls.filter(
        (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/accounts"),
      ).length;
    expect(acctsAfter).toBeGreaterThan(acctsBefore);

    // Retry button is visible and enabled.
    const retry = screen.getByRole("button", { name: /retry/i });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
  });

  it("dismisses the banner and re-issues the reloads when Retry is clicked", async () => {
    let txCalls = 0;
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
      if (url.startsWith("/api/v1/categories")) return [] as never;
      if (url.startsWith("/api/v1/budgets")) return [] as never;
      if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
      if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 } as never;
      if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
      if (url.startsWith("/api/v1/forecast/account-balances")) return null as never;
      if (url.startsWith("/api/v1/forecast")) {
        return {
          period_start: "2026-05-01",
          period_end: "2026-05-31",
          executed_income: "0",
          executed_expense: "0",
          executed_net: "0",
          pending_income: "0",
          pending_expense: "0",
          recurring_income: "0",
          recurring_expense: "0",
          forecast_income: "0",
          forecast_expense: "0",
          forecast_net: "0",
          categories: [],
        } as never;
      }
      if (url.startsWith("/api/v1/transactions")) {
        txCalls += 1;
        // Initial load and the retry succeed; the post-event refresh
        // (call #2) is the only failure. After Retry the banner
        // clears.
        if (txCalls === 2) throw new Error("transient");
        return { items: [], total: 0, limit: 200, offset: 0 } as never;
      }
      return null as never;
    });

    render(<DashboardPage />);
    await waitFor(() => {
      expect(txCalls).toBeGreaterThanOrEqual(1);
    });

    act(() => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    await waitFor(() => {
      expect(screen.getByTestId("dashboard-refresh-error")).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(screen.queryByTestId("dashboard-refresh-error")).toBeNull();
    });
  });
});
