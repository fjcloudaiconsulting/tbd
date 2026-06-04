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
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
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
];

function mockApi() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    return Promise.resolve({});
  }) as never);
}

describe("AccountsPage — side-by-side layout (post-#199 follow-up)", () => {
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
    mockApi();
  });

  it("wraps the two cards in a 3-column grid at lg+ that stacks below lg", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    const grid = screen.getByTestId("accounts-page-grid");
    expect(grid).toBeInTheDocument();
    // Default (below lg) keeps the stacked column layout introduced by
    // PR #199 so mobile + tablet match the shape users already learned.
    expect(grid.className).toMatch(/\bflex-col\b/);
    // lg+ promotes to a 3-column grid. Account Types takes 1/3 and
    // Accounts takes 2/3 — see the next assertion for span checks.
    expect(grid.className).toMatch(/\blg:grid\b/);
    expect(grid.className).toMatch(/\blg:grid-cols-3\b/);
    // items-start so the Types card keeps its intrinsic height instead
    // of stretching to match the taller Accounts card.
    expect(grid.className).toMatch(/\blg:items-start\b/);
  });

  it("places Account Types in col-span-1 and Accounts in col-span-2 at lg+", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    const grid = screen.getByTestId("accounts-page-grid");
    const [typesCard, accountsCard] = Array.from(grid.children) as HTMLElement[];

    // Two direct children: Types card first (DOM order keeps mobile
    // stack visually identical to post-#199), Accounts second.
    expect(grid.children.length).toBe(2);
    expect(typesCard.className).toMatch(/\blg:col-span-1\b/);
    expect(accountsCard.className).toMatch(/\blg:col-span-2\b/);
  });

  it("still renders the Account Types and Accounts cards with all controls", async () => {
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    // Account Types card content survived the reflow.
    expect(screen.getByRole("heading", { name: /Account Types/ })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/New type name/)).toBeInTheDocument();

    // Accounts card actions survived (Add Account toggle + row actions).
    expect(screen.getByRole("button", { name: /\+ Add Account/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Edit Amex Primary$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Delete Amex Primary$/ })).toBeInTheDocument();

    // Page-title HelpAnchor still present (link with `Help: Accounts`).
    expect(
      screen.getByRole("link", { name: /Help: Accounts/ }),
    ).toBeInTheDocument();
  });
});
