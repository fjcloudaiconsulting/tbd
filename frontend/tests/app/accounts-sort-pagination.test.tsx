import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

const ACCOUNT_TYPES = [
  { id: 1, name: "Credit Card", slug: "credit_card", is_system: true, account_count: 1 },
  { id: 2, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
  { id: 3, name: "Savings", slug: "savings", is_system: true, account_count: 1 },
];

// Names deliberately not in alphabetical order, balances spread so each
// sort key produces a distinct ordering we can assert against.
const ACCOUNTS = [
  {
    id: 10,
    name: "Charlie",
    account_type_id: 2,
    account_type_name: "Checking",
    account_type_slug: "checking",
    balance: "500.00",
    currency: "EUR",
    is_active: true,
    is_default: false,
    close_day: null,
  },
  {
    id: 20,
    name: "alpha",
    account_type_id: 1,
    account_type_name: "Credit Card",
    account_type_slug: "credit_card",
    balance: "-100.00",
    currency: "EUR",
    is_active: true,
    is_default: true,
    close_day: 5,
  },
  {
    id: 30,
    name: "Bravo",
    account_type_id: 3,
    account_type_name: "Savings",
    account_type_slug: "savings",
    balance: "2000.00",
    currency: "EUR",
    is_active: true,
    is_default: false,
    close_day: null,
  },
];

function mockApi(accounts: unknown[] = ACCOUNTS) {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (url === "/api/v1/accounts") return Promise.resolve(accounts);
    if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    return Promise.resolve({});
  }) as never);
}

function setAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

// Visible account rows in DOM order, by the name in each article.
function rowNames(): string[] {
  return screen
    .getAllByTestId(/^account-row-\d+$/)
    .map((el) => el.getAttribute("data-account-name") ?? "");
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(apiFetch).mockReset();
  setAuth();
  mockApi();
});

describe("AccountsPage — sortable columns", () => {
  it("defaults to name ascending (case-insensitive)", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Charlie/)).toBeInTheDocument());
    // alpha, Bravo, Charlie — case-insensitive ascending.
    expect(rowNames()).toEqual(["alpha", "Bravo", "Charlie"]);
  });

  it("toggles name to descending on second click of the Account header", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Charlie/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /^Account/ }));
    expect(rowNames()).toEqual(["Charlie", "Bravo", "alpha"]);
  });

  it("sorts by type ascending then descending", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Charlie/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /^Type/ }));
    // Checking, Credit Card, Savings -> Charlie, alpha, Bravo
    expect(rowNames()).toEqual(["Charlie", "alpha", "Bravo"]);
    fireEvent.click(screen.getByRole("button", { name: /^Type/ }));
    expect(rowNames()).toEqual(["Bravo", "alpha", "Charlie"]);
  });

  it("sorts by balance numerically", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Charlie/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /^Balance/ }));
    // -100 (alpha), 500 (Charlie), 2000 (Bravo)
    expect(rowNames()).toEqual(["alpha", "Charlie", "Bravo"]);
    fireEvent.click(screen.getByRole("button", { name: /^Balance/ }));
    expect(rowNames()).toEqual(["Bravo", "Charlie", "alpha"]);
  });
});

describe("AccountsPage — page clamping when row count shrinks", () => {
  function manyAccounts(n: number) {
    return Array.from({ length: n }, (_, i) => ({
      id: 100 + i,
      name: `Acct ${String(i).padStart(3, "0")}`,
      account_type_id: 2,
      account_type_name: "Checking",
      account_type_slug: "checking",
      balance: String(i),
      currency: "EUR",
      is_active: true,
      is_default: false,
      close_day: null,
    }));
  }

  it("shows remaining rows (not blank) after a delete shrinks below page-2 threshold", async () => {
    // Start: 30 accounts => page 1 of 2. Navigate to page 2 (5 rows).
    // Delete one account; reload returns 24 accounts (all fit on page 1).
    // The table must render 24 rows, not blank.
    const initialAccounts = manyAccounts(30);
    const afterDeleteAccounts = manyAccounts(24);

    // First round: return 30 accounts.
    vi.mocked(apiFetch).mockImplementation(((url: string, opts?: RequestInit) => {
      const method = opts?.method?.toUpperCase() ?? "GET";
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts" && method === "GET") return Promise.resolve(initialAccounts);
      if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve({});
    }) as never);

    render(<AccountsPage />);

    // Wait for first 25 rows.
    await waitFor(() => expect(screen.getByText("Acct 024")).toBeInTheDocument());
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();

    // Navigate to page 2.
    fireEvent.click(screen.getByRole("button", { name: /Next page/ }));
    await waitFor(() => expect(screen.getByText("Acct 025")).toBeInTheDocument());
    expect(screen.queryByText("Acct 000")).toBeNull();

    // Reconfigure mock: next accounts GET returns 24, DELETE succeeds.
    vi.mocked(apiFetch).mockImplementation(((url: string, opts?: RequestInit) => {
      const method = opts?.method?.toUpperCase() ?? "GET";
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts" && method === "GET") return Promise.resolve(afterDeleteAccounts);
      if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      if (url.match(/\/api\/v1\/accounts\/\d+/) && method === "DELETE") return Promise.resolve({});
      return Promise.resolve({});
    }) as never);

    // Delete the first row visible on page 2 (Acct 025, id=125).
    // Delete now lives in the per-row "..." overflow menu, so open it
    // first, then click the Delete menu item (aria-label "Delete Acct 025").
    fireEvent.click(
      screen.getByRole("button", { name: /More actions for Acct 025/ }),
    );
    const deleteItem = await screen.findByRole("menuitem", {
      name: /Delete Acct 025/,
    });
    fireEvent.click(deleteItem);

    // ConfirmModal appears — click its Delete confirmation button (last "Delete").
    await screen.findByText(/Delete this account/);
    const allDeleteBtns = screen.getAllByRole("button", { name: /^Delete$/ });
    fireEvent.click(allDeleteBtns[allDeleteBtns.length - 1]);

    // After reload with 24 accounts, page clamped to 1 — all 24 should be visible.
    await waitFor(() => expect(screen.getByText("Acct 000")).toBeInTheDocument());
    // Ensure it's not blank: row count should be 24 (all on one page).
    const rows = screen.getAllByTestId(/^account-row-\d+$/);
    expect(rows.length).toBe(24);
  });
});

describe("AccountsPage — pagination", () => {
  function manyAccounts(n: number) {
    return Array.from({ length: n }, (_, i) => ({
      id: 100 + i,
      // Zero-padded so name-ascending order is the index order.
      name: `Acct ${String(i).padStart(3, "0")}`,
      account_type_id: 2,
      account_type_name: "Checking",
      account_type_slug: "checking",
      balance: String(i),
      currency: "EUR",
      is_active: true,
      is_default: false,
      close_day: null,
    }));
  }

  it("does not render pagination when rows fit one page", async () => {
    mockApi(manyAccounts(3));
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText("Acct 000")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Next page/ })).toBeNull();
  });

  it("paginates and Next/Previous change the visible rows", async () => {
    mockApi(manyAccounts(30)); // > default page size 25
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText("Acct 000")).toBeInTheDocument());

    // Page 1: first 25 (Acct 000 .. Acct 024). Acct 025 not yet visible.
    expect(screen.getByText("Acct 024")).toBeInTheDocument();
    expect(screen.queryByText("Acct 025")).toBeNull();
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Next page/ }));

    await waitFor(() => expect(screen.getByText("Acct 025")).toBeInTheDocument());
    expect(screen.queryByText("Acct 000")).toBeNull();
    expect(screen.getByText(/Page 2 of 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Previous page/ }));
    await waitFor(() => expect(screen.getByText("Acct 000")).toBeInTheDocument());
    expect(screen.queryByText("Acct 025")).toBeNull();
  });
});

describe("AccountsPage — nulls-last stable sort", () => {
  const ACCOUNTS_WITH_EMPTY_TYPE = [
    {
      id: 10,
      name: "Charlie",
      account_type_id: 2,
      account_type_name: "",
      account_type_slug: "checking",
      balance: "500.00",
      currency: "EUR",
      is_active: true,
      is_default: false,
      close_day: null,
    },
    {
      id: 20,
      name: "alpha",
      account_type_id: 1,
      account_type_name: "Zeta",
      account_type_slug: "credit_card",
      balance: "-100.00",
      currency: "EUR",
      is_active: true,
      is_default: true,
      close_day: 5,
    },
    {
      id: 30,
      name: "Bravo",
      account_type_id: 3,
      account_type_name: "Alpha",
      account_type_slug: "savings",
      balance: "2000.00",
      currency: "EUR",
      is_active: true,
      is_default: false,
      close_day: null,
    },
  ];

  beforeEach(() => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS_WITH_EMPTY_TYPE);
      if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve({});
    }) as never);
  });

  it("keeps empty account_type_name last when sorting by Type descending", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Charlie/)).toBeInTheDocument());

    // Click Type once: asc => Alpha (Bravo), Zeta (alpha), empty (Charlie)
    fireEvent.click(screen.getByRole("button", { name: /^Type/ }));
    expect(rowNames()).toEqual(["Bravo", "alpha", "Charlie"]);

    // Click Type again: desc => Zeta (alpha), Alpha (Bravo), empty (Charlie) still last
    fireEvent.click(screen.getByRole("button", { name: /^Type/ }));
    expect(rowNames()).toEqual(["alpha", "Bravo", "Charlie"]);
  });
});
