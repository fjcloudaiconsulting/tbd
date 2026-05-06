import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
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
  role: "owner",
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

const PLAN_1000 = {
  id: 1,
  billing_period_id: 0,
  period_start: "",
  period_end: null,
  status: "active",
  total_planned_income: "0",
  total_planned_expense: "1000",
  total_actual_income: "0",
  total_actual_expense: "0",
  items: [],
};

describe("DashboardPage — projection race protection", () => {
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

  // Newest-first ordering: index 0 = current (May), index 1 = past (April).
  // The dashboard's "Previous period" button increments periodIdx, so a
  // single click moves us from May (index 0) to April (index 1).
  const periods = [
    { id: 2, start_date: "2026-05-01", end_date: null },
    { id: 1, start_date: "2026-04-01", end_date: "2026-04-30" },
  ];

  it("ignores a stale projection response after the period has changed", async () => {
    let resolveMay!: (v: unknown) => void;
    const mayProjection = new Promise<unknown>((r) => {
      resolveMay = r;
    });

    // Stale May data — would render ON TRACK (200/1000 = 0.2) if it were
    // committed. The race fix must reject this response after the user
    // has switched to April.
    const mayStaleData = {
      period_start: "2026-05-01",
      period_end: "2026-05-31",
      executed_income: "0",
      executed_expense: "200",
      executed_net: "0",
      pending_income: "0",
      pending_expense: "0",
      recurring_income: "0",
      recurring_expense: "0",
      forecast_income: "0",
      forecast_expense: "200",
      forecast_net: "0",
      categories: [],
    };

    // April actuals: 1500 vs plan of 1000 → ENDED OVER BUDGET.
    const aprilData = {
      period_start: "2026-04-01",
      period_end: "2026-04-30",
      executed_income: "0",
      executed_expense: "1500",
      executed_net: "-1500",
      pending_income: "0",
      pending_expense: "0",
      recurring_income: "0",
      recurring_expense: "0",
      forecast_income: "0",
      forecast_expense: "1500",
      forecast_net: "-1500",
      categories: [],
    };

    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url === "/api/v1/categories") return Promise.resolve([]);
      if (url.startsWith("/api/v1/budgets")) return Promise.resolve([]);
      if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period") return Promise.resolve(periods[0]);
      if (url === "/api/v1/settings/billing-periods") return Promise.resolve(periods);
      if (url.startsWith("/api/v1/transactions")) return Promise.resolve([]);
      if (url.includes("forecast-plans/current"))
        return Promise.resolve({ ...PLAN_1000, period_start: url.includes("2026-04-01") ? "2026-04-01" : "2026-05-01" });
      // The race: May's projection is held; April's resolves immediately.
      if (url === "/api/v1/forecast?period_start=2026-05-01") return mayProjection;
      if (url === "/api/v1/forecast?period_start=2026-04-01") return Promise.resolve(aprilData);
      return Promise.resolve({});
    }) as never);

    render(<DashboardPage />);

    // Wait for initial render with the May period selected and the hero mounted.
    await waitFor(() => expect(screen.getByTestId("on-track-tile")).toBeInTheDocument());

    // May's projection request is in flight.
    // Switch to April (one click "Previous period").
    fireEvent.click(screen.getByLabelText(/previous period/i));

    // April resolves synchronously (the mock returns Promise.resolve).
    // Wait for the past-period verdict to render.
    await waitFor(
      () => expect(screen.getByText(/ENDED OVER BUDGET/)).toBeInTheDocument(),
      { timeout: 3000 },
    );

    // Now resolve May's stale promise. If the race fix is in place, the
    // late commit must be discarded; the tile must continue to show April.
    resolveMay(mayStaleData);

    // Flush microtasks + any setState propagation.
    await new Promise((r) => setTimeout(r, 50));

    // The hero MUST still show April's verdict, not May's.
    expect(screen.getByText(/ENDED OVER BUDGET/)).toBeInTheDocument();
    // ON TRACK should not appear anywhere in the hero.
    expect(screen.queryByText(/^ON TRACK$/)).not.toBeInTheDocument();
    // PROJECTED column doesn't render for past periods.
    expect(screen.queryByText(/^PROJECTED$/)).not.toBeInTheDocument();
  });
});
