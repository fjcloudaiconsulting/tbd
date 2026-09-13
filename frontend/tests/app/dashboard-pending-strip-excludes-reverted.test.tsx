/**
 * TBD-272 fix (c): the dashboard pending strip ignores reverted rows.
 *
 * A skipped recurring occurrence stays PENDING forever, so the all-time
 * `?status=pending` fetch returns it. Its amount will never land; counting it
 * in the account tile's "Pending:" line overstates what is committed.
 *
 * FENCE. Kills a reduce that sums every pending row: the tile would read
 * 100.00 (30 + 70) instead of 30.00. Amounts are asymmetric on purpose.
 */
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
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner", org_id: 1,
  org_name: "Acme", billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null, trial_end: null,
};

const ACCOUNT = {
  id: 10, name: "Main", account_type_id: 1, account_type_name: "Checking",
  account_type_slug: "checking", balance: 1000, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const PENDING = {
  account_id: 10, type: "expense", status: "pending", date: "2026-05-20",
  category_id: 1, category_name: "Rent", account_name: "Main",
  linked_transaction_id: null, is_imported: false, settled_date: null,
  is_manual_adjustment: false, recurring_id: 7, tags: [],
};

describe("DashboardPage — pending strip ignores reverted rows (TBD-272)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never, loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    } as never);
  });

  it("totals only the non-reverted pending rows", async () => {
    const pendingItems = [
      { ...PENDING, id: 1, description: "Rent", amount: "30.00", is_reverted: false },
      { ...PENDING, id: 2, description: "Skipped rent", amount: "70.00", is_reverted: true },
    ];
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([ACCOUNT]);
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
      if (url.startsWith("/api/v1/forecast/account-balances"))
        return Promise.resolve({ totals: [], accounts: [] });
      if (url.startsWith("/api/v1/transactions?status=pending"))
        return Promise.resolve({ items: pendingItems, total: 2, limit: 200, offset: 0 });
      if (url.startsWith("/api/v1/transactions?"))
        return Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 });
      return Promise.resolve({});
    }) as never);

    render(<DashboardPage />);

    const pending = await waitFor(() => screen.getByLabelText("Pending, not yet settled"), {
      timeout: 3000,
    });
    expect(pending.textContent).toContain("30.00");
    expect(pending.textContent).not.toContain("100.00");
  });
});
