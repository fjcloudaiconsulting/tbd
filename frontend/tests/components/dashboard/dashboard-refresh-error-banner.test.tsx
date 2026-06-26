import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import CustomDashboard from "@/components/dashboard/CustomDashboard";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

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
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn().mockResolvedValue([]) };
});

vi.mock("@/lib/hooks/use-persisted-sort", async () => {
  const actual = await vi.importActual<typeof import("@/lib/hooks/use-persisted-sort")>("@/lib/hooks/use-persisted-sort");
  return {
    ...actual,
    usePersistedSort: vi.fn(() => ({ field: "date", dir: "desc", setSort: vi.fn(), reset: vi.fn(), isDefault: true })),
  };
});

vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
  getDefaultDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
  saveDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
}));

vi.mock("@/lib/reports/use-filter-chip-state", () => ({
  useFilterChipState: vi.fn(() => ({ accounts: [] })),
}));

vi.mock("@/components/reports/Canvas", () => ({
  default: () => <div data-testid="canvas" />,
}));

vi.mock("@/components/dashboard/DashboardPeriodNav", () => ({
  default: () => <div data-testid="period-nav" />,
}));

const USER = {
  id: 1, username: "u", email: "u@x.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const,
  org_id: 1, org_name: "Acme", billing_cycle_day: 1,
  is_superadmin: false, is_active: true, mfa_enabled: false,
  subscription_status: null, subscription_plan: null, trial_end: null,
};

const PERIOD = { id: 1, start_date: "2026-05-01", end_date: null };

function makeHandler(opts: { failOnRefresh?: boolean } = {}) {
  // loadRefs() fetches accounts, and it does NOT catch internally — so a
  // failure on the accounts endpoint propagates out of loadRefs() and is
  // visible to Promise.allSettled() in refresh(). We count accounts calls
  // and fail on the second call (the one triggered by refresh()).
  let accountCalls = 0;
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) {
      accountCalls += 1;
      if (opts.failOnRefresh && accountCalls >= 2) throw new Error("backend hiccup");
      return [] as never;
    }
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
    if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 } as never;
    if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
    if (url.startsWith("/api/v1/forecast/account-balances")) return null as never;
    if (url.startsWith("/api/v1/forecast")) return {
      period_start: "2026-05-01", period_end: "2026-05-31",
      executed_income: "0", executed_expense: "0", executed_net: "0",
      pending_income: "0", pending_expense: "0", recurring_income: "0",
      recurring_expense: "0", forecast_income: "0", forecast_expense: "0",
      forecast_net: "0", categories: [],
    } as never;
    if (url.startsWith("/api/v1/transactions")) {
      return { items: [], total: 0, limit: 200, offset: 0 } as never;
    }
    return null as never;
  };
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    features: { customDashboard: true } as never,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

describe("CustomDashboard refresh-error banner", () => {
  it("does not show the banner on a clean post-write refresh", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler());
    render(<CustomDashboard />);
    // Wait for initial load — accounts is called in loadRefs which is the
    // first fetch on mount. After it resolves, loading becomes false.
    await waitFor(() => {
      expect(
        vi.mocked(apiFetch).mock.calls.some((c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/accounts")),
      ).toBe(true);
    });
    act(() => { window.dispatchEvent(new Event("pfv:transaction-added")); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.queryByTestId("dashboard-refresh-error")).toBeNull();
  });

  it("surfaces an inline retry affordance when a refresh call rejects", async () => {
    // makeHandler({ failOnRefresh: true }) fails /accounts on the 2nd call.
    // loadRefs() fetches /accounts without an internal try/catch, so the
    // rejection propagates to Promise.allSettled() in refresh() and sets
    // refreshError=true — surfacing the error banner.
    vi.mocked(apiFetch).mockImplementation(makeHandler({ failOnRefresh: true }));
    render(<CustomDashboard />);
    // Wait for initial mount to complete (first accounts call succeeds)
    await waitFor(() => {
      const accountCalls = vi.mocked(apiFetch).mock.calls.filter(
        (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/accounts"),
      ).length;
      expect(accountCalls).toBeGreaterThanOrEqual(1);
    });
    // Trigger a post-write refresh — accounts call #2 will fail
    act(() => { window.dispatchEvent(new Event("pfv:transaction-added")); });
    await waitFor(() => {
      expect(screen.getByTestId("dashboard-refresh-error")).toBeTruthy();
    });
    const retry = screen.getByRole("button", { name: /retry/i });
    expect((retry as HTMLButtonElement).disabled).toBe(false);
  });

  it("clears the banner and re-runs refresh when Retry is clicked", async () => {
    // Fail accounts on call #2 only — triggers the error banner.
    // On call #3 (triggered by Retry), accounts succeeds again → banner clears.
    let accountCalls = 0;
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) {
        accountCalls += 1;
        if (accountCalls === 2) throw new Error("transient");
        return [] as never;
      }
      if (url.startsWith("/api/v1/categories")) return [] as never;
      if (url.startsWith("/api/v1/budgets")) return [] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
      if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
      if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 } as never;
      if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
      if (url.startsWith("/api/v1/forecast/account-balances")) return null as never;
      if (url.startsWith("/api/v1/forecast")) return {
        period_start: "2026-05-01", period_end: "2026-05-31",
        executed_income: "0", executed_expense: "0", executed_net: "0",
        pending_income: "0", pending_expense: "0", recurring_income: "0",
        recurring_expense: "0", forecast_income: "0", forecast_expense: "0",
        forecast_net: "0", categories: [],
      } as never;
      if (url.startsWith("/api/v1/transactions")) {
        return { items: [], total: 0, limit: 200, offset: 0 } as never;
      }
      return null as never;
    });
    render(<CustomDashboard />);
    await waitFor(() => expect(accountCalls).toBeGreaterThanOrEqual(1));
    act(() => { window.dispatchEvent(new Event("pfv:transaction-added")); });
    await waitFor(() => { expect(screen.getByTestId("dashboard-refresh-error")).toBeTruthy(); });
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => { expect(screen.queryByTestId("dashboard-refresh-error")).toBeNull(); });
  });
});
