import { screen, waitFor, within } from "@testing-library/react";
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

const BASE_USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
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
};

const ACCOUNT_TYPES = [
  { id: 1, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
];

const ACCOUNTS = [
  {
    id: 10,
    name: "Primary",
    account_type_id: 1,
    account_type_name: "Checking",
    account_type_slug: "checking",
    balance: "150.00",
    currency: "EUR",
    is_active: true,
    is_default: true,
    close_day: null,
  },
];

function mockApi() {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path.startsWith("/api/v1/accounts")) return Promise.resolve(ACCOUNTS);
    if (path.startsWith("/api/v1/transactions")) {
      // fetchAll reads `.items` off the list envelope.
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    }
    return Promise.resolve([]);
  });
}

function setUser(role: "owner" | "admin" | "member", flag: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...BASE_USER, role, allow_manual_balance_adjustment: flag } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("AccountsPage — Adjust balance button gating (Track E)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    mockApi();
  });

  it("hides the Adjust balance button when allow_manual_balance_adjustment is false", async () => {
    setUser("owner", false);
    renderWithSWR(<AccountsPage />);
    await waitFor(() => {
      expect(screen.getByText("Primary")).toBeTruthy();
    });
    expect(screen.queryByRole("button", { name: /Adjust balance of Primary/i })).toBeNull();
  });

  it("hides the Adjust balance button for non-admin users even when the flag is true", async () => {
    setUser("member", true);
    renderWithSWR(<AccountsPage />);
    await waitFor(() => {
      expect(screen.getByText("Primary")).toBeTruthy();
    });
    expect(screen.queryByRole("button", { name: /Adjust balance of Primary/i })).toBeNull();
  });

  it("shows the Adjust balance button when admin AND flag is true", async () => {
    setUser("admin", true);
    renderWithSWR(<AccountsPage />);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Adjust balance of Primary/i })
      ).toBeTruthy();
    });
  });

  it("keeps header/row grid templates aligned in the wider Adjust-balance (12rem) branch", async () => {
    // When canAdjustBalance is true the action column widens to 12rem. The
    // header <tr> and each row <article> must still carry the IDENTICAL
    // md:grid-cols-* template, otherwise the columns drift (the misalignment
    // bug this refactor fixes). The other (5rem) branch is covered in
    // accounts-list-headers.test.tsx.
    setUser("admin", true);
    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getByText("Primary")).toBeTruthy());

    const gridCols = (el: HTMLElement) =>
      Array.from(el.classList).find((c) => c.startsWith("md:grid-cols-"));
    const headerRow = within(
      screen.getByTestId("accounts-list-header"),
    ).getByRole("row");
    const row = screen.getByTestId("account-row-10");

    expect(gridCols(headerRow)).toContain("12rem");
    expect(gridCols(row)).toBe(gridCols(headerRow));
  });
});
