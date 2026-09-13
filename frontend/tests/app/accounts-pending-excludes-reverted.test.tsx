/**
 * TBD-272: the accounts page "Pending:" line ignores reverted rows.
 *
 * FENCE. Kills a reduce that sums every pending row: a skipped recurring
 * occurrence stays PENDING forever, so the line would read €200.00 (120 + 80)
 * instead of €120.00. Amounts are asymmetric on purpose.
 */
import { screen, waitFor } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import AccountsPage from "@/app/accounts/page";
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
  usePathname: () => "/accounts",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner", org_id: 1,
  org_name: "Acme", billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null, trial_end: null,
};

const ACCOUNT_TYPES = [
  { id: 2, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
];

const ACCOUNTS = [
  {
    id: 20, name: "ING Joint", account_type_id: 2, account_type_name: "Checking",
    account_type_slug: "checking", balance: "1500.00", currency: "EUR",
    is_active: true, is_default: true, close_day: null,
  },
];

const ROW = {
  account_id: 20, type: "expense", status: "pending", date: "2026-04-15",
  category_id: null, category_name: null, account_name: "ING Joint", currency: "EUR",
  linked_transaction_id: null, is_imported: false, settled_date: null, recurring_id: 7,
};

describe("AccountsPage — pending line ignores reverted rows (TBD-272)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never, loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    } as never);
  });

  it("totals only the non-reverted pending rows", async () => {
    const pending = [
      { ...ROW, id: 1, description: "Rent", amount: "120.00", is_reverted: false },
      { ...ROW, id: 2, description: "Skipped rent", amount: "80.00", is_reverted: true },
    ];
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
      if (url.startsWith("/api/v1/transactions?status=pending"))
        return Promise.resolve({ items: pending, total: 2, limit: 200, offset: 0 });
      return Promise.resolve({});
    }) as never);

    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getAllByText(/^Pending:/).length).toBeGreaterThan(0));
    expect(screen.getByText(/^Pending: €120\.00$/)).toBeInTheDocument();
    expect(screen.queryByText(/€200\.00/)).not.toBeInTheDocument();
  });
});
