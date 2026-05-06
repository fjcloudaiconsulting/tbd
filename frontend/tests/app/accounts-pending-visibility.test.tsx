import { render, screen, waitFor } from "@testing-library/react";

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

const ACCOUNT_TYPES = [
  { id: 1, name: "Credit Card", slug: "credit_card", is_system: true, account_count: 1 },
  { id: 2, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
];

const ACCOUNTS = [
  {
    id: 10,
    name: "Amex Primary",
    account_type_id: 1,
    account_type_name: "Credit Card",
    account_type_slug: "credit_card",
    balance: "0.00",
    currency: "EUR",
    is_active: true,
    is_default: false,
    close_day: 5,
  },
  {
    id: 20,
    name: "ING Joint",
    account_type_id: 2,
    account_type_name: "Checking",
    account_type_slug: "checking",
    balance: "1500.00",
    currency: "EUR",
    is_active: true,
    is_default: true,
    close_day: null,
  },
];

describe("AccountsPage — pending visibility (L3.4)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
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

  function mockAccountsAPI(pending: unknown[]) {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
      // fetchAll<T> appends &limit=200&offset=N; match the prefix.
      if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve(pending);
      return Promise.resolve({});
    }) as never);
  }

  it("renders no Pending line when there are no pending transactions", async () => {
    mockAccountsAPI([]);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());
    expect(screen.queryByText(/^Pending:/)).not.toBeInTheDocument();
  });

  it("renders Pending: line for a CC with a pending charge, even when balance is 0", async () => {
    // The exact L3.4 scenario: CC balance is 0 (post-payment) but pending != 0.
    // Pre-fix, this row showed nothing about the pending charge.
    mockAccountsAPI([
      {
        id: 100,
        account_id: 10,
        amount: "150.00",
        type: "expense",
        status: "pending",
        date: "2026-04-15",
        description: "Pending charge",
        category_id: null,
        category_name: null,
        account_name: "Amex Primary",
        currency: "EUR",
        linked_transaction_id: null,
        is_imported: false,
        settled_date: null,
      },
    ]);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Pending: 150\.00/)).toBeInTheDocument());
    // The CC tile keeps its 0.00 balance display.
    expect(screen.getAllByText(/0\.00/).length).toBeGreaterThan(0);
    // The Checking account, with no pending transactions, must NOT render a Pending line.
    // Tighter check: only one Pending: text on the page.
    expect(screen.getAllByText(/^Pending:/)).toHaveLength(1);
  });

  it("renders accounts even when the pending fetch rejects (best-effort)", async () => {
    // The pending augment must not bring the page down. Account types +
    // accounts succeed; pending throws. Page must still render the
    // accounts list and clear the spinner.
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
      if (url.startsWith("/api/v1/transactions?status=pending"))
        return Promise.reject(new Error("backend down"));
      return Promise.resolve({});
    }) as never);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());
    expect(screen.getByText(/ING Joint/)).toBeInTheDocument();
    // Without the best-effort handling, the spinner would never clear
    // and Amex Primary would never render.
    expect(screen.queryByText(/^Pending:/)).not.toBeInTheDocument();
  });

  it("aggregates multiple pending charges per account and ignores other accounts", async () => {
    mockAccountsAPI([
      { id: 1, account_id: 10, amount: "120.00", type: "expense", status: "pending", date: "2026-04-15", description: "a", category_id: null, category_name: null, account_name: "Amex Primary", currency: "EUR", linked_transaction_id: null, is_imported: false, settled_date: null },
      { id: 2, account_id: 10, amount: "30.00", type: "expense", status: "pending", date: "2026-04-16", description: "b", category_id: null, category_name: null, account_name: "Amex Primary", currency: "EUR", linked_transaction_id: null, is_imported: false, settled_date: null },
    ]);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Pending: 150\.00/)).toBeInTheDocument());
  });
});
