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

function mockDashboardWithOneTx() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?"))
      return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/transactions?status=pending"))
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    if (url.startsWith("/api/v1/transactions")) {
      const items = [
        {
          id: 1,
          account_id: 10,
          amount: "10.00",
          type: "expense",
          status: "settled",
          date: "2026-05-01",
          description: "Coffee",
          category_id: null,
          category_name: null,
          account_name: "Checking",
          currency: "EUR",
          linked_transaction_id: null,
          is_imported: false,
          settled_date: "2026-05-01",
        },
      ];
      return Promise.resolve({ items, total: items.length, limit: 200, offset: 0 });
    }
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage — Recent Transactions sort-header tap targets (WCAG 2.5.8)", () => {
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
    mockDashboardWithOneTx();
  });

  it("Date, Description, Amount sort buttons carry min-h-[32px]", async () => {
    render(<DashboardPage />);

    // Wait until the inline mini-header buttons have rendered.
    await waitFor(() => {
      const labels = screen.queryAllByRole("button").map((b) => b.textContent ?? "");
      const hasDate = labels.some((t) => t.startsWith("Date"));
      const hasDescription = labels.some((t) => t.startsWith("Description"));
      const hasAmount = labels.some((t) => t.startsWith("Amount"));
      if (!(hasDate && hasDescription && hasAmount)) {
        throw new Error("sort-header buttons not rendered yet");
      }
    });

    const buttons = screen.getAllByRole("button");
    const findHeader = (name: string) => {
      const btn = buttons.find((b) => (b.textContent ?? "").startsWith(name));
      if (!btn) throw new Error(`No sort header for "${name}"`);
      return btn;
    };

    for (const name of ["Date", "Description", "Amount"]) {
      const btn = findHeader(name);
      expect(btn.className).toContain("min-h-[32px]");
    }
  });
});
