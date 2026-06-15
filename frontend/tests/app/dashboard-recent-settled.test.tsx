import { render, screen, waitFor, within } from "@testing-library/react";

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
  email_verified: true, role: "owner", org_id: 1, org_name: "Acme",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const GBLT = {
  id: 1, account_id: 10, amount: "12.00", type: "expense", status: "settled",
  date: "2026-05-31", description: "GBLT", category_id: 1,
  category_name: "Groceries", account_name: "Checking", currency: "EUR",
  linked_transaction_id: null, is_imported: false, settled_date: "2026-06-15",
};

const PENDING = {
  id: 2, account_id: 10, amount: "50.00", type: "expense", status: "pending",
  date: "2026-06-10", description: "Pending", category_id: 1,
  category_name: "Groceries", account_name: "Credit", currency: "EUR",
  linked_transaction_id: null, is_imported: false, settled_date: null,
};

const TXS = [GBLT, PENDING];

function mockDashboard() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?")) return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [PENDING], total: 1, limit: 200, offset: 0 });
    if (url.startsWith("/api/v1/transactions")) return Promise.resolve({ items: TXS, total: TXS.length, limit: 200, offset: 0 });
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage Recent Transactions — settled date (Task 9)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    window.localStorage.clear();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    mockDashboard();
  });

  it("surfaces the settled date on a recent transaction row", async () => {
    render(<DashboardPage />);

    const settled = await screen.findByTestId("dash-settled-1");
    // Settled date (June 15) is visible on the GBLT row.
    expect(settled).toHaveTextContent("06-15");
    // The original date (May 31) is also still present on the row.
    expect(screen.getAllByText(/05-31/).length).toBeGreaterThan(0);
  });

  it("shows an em-dash placeholder when a row has no settled date", async () => {
    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByTestId("dash-settled-2")).toBeInTheDocument();
    });
    expect(within(screen.getByTestId("dash-settled-2")).getByText("—")).toBeInTheDocument();
  });
});
