import { render, screen, waitFor } from "@testing-library/react";

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
  id: 1, username: "u", email: "u@x.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1,
  is_superadmin: false, is_active: true, mfa_enabled: false,
  subscription_status: null, subscription_plan: null, trial_end: null,
};

function mockEmptyDashboard() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets") return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods") return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/transactions")) return Promise.resolve([]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage — forecast info tooltip (L3.7)", () => {
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
    mockEmptyDashboard();
  });

  it("renders an info disclosure next to the Forecast heading", async () => {
    render(<DashboardPage />);
    await waitFor(() =>
      expect(screen.getByLabelText(/explain forecast numbers/i)).toBeInTheDocument(),
    );
  });

  it("includes both planned and actual explanation text in the tooltip body", async () => {
    render(<DashboardPage />);
    await waitFor(() =>
      expect(screen.getByLabelText(/explain forecast numbers/i)).toBeInTheDocument(),
    );
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toBeInTheDocument();
    expect(tooltip.textContent).toMatch(/planned/i);
    expect(tooltip.textContent).toMatch(/forecast plan/i);
    expect(tooltip.textContent).toMatch(/actual/i);
    // Pin the corrected financial semantic: actuals are settled-only,
    // matched by settlement date. The earlier copy claimed "settled +
    // pending" which contradicted backend/forecast_plan_service.py
    // (Transaction.status == SETTLED + Transaction.settled_date filter).
    expect(tooltip.textContent).toMatch(/settled transactions/i);
    expect(tooltip.textContent).toMatch(/settlement date/i);
    expect(tooltip.textContent).toMatch(/pending.*not counted/i);
  });
});
