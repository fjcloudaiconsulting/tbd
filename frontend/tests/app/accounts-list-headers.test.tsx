import { fireEvent, screen, waitFor, within } from "@testing-library/react";
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

// Two rows: one default, one non-default. The fixed-slot grid must
// produce identical action-column class lists for both rows, otherwise
// "Set default" disappearing on the default row would let the remaining
// links shift left (the bug PR #172 left behind on the accounts list).
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

function mockApi() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (url === "/api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    return Promise.resolve({});
  }) as never);
}

describe("AccountsPage — list header row and fixed action column", () => {
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

  it("renders sortable Account / Type / Balance headers above the accounts list", async () => {
    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    const header = screen.getByTestId("accounts-list-header");
    expect(header).toBeInTheDocument();
    // Hidden on mobile, table on md+ so it can host the shared
    // SortableHeader <th> cells (post-migration to the shared
    // sort+pagination building blocks). It was a plain md:grid label
    // strip before; now each label is a sort button.
    expect(header.className).toContain("hidden");
    expect(header.className).toContain("md:table");

    // Each sortable column renders an accessible button with the label.
    expect(
      within(header).getByRole("button", { name: /^Account$/ }),
    ).toBeInTheDocument();
    expect(
      within(header).getByRole("button", { name: /^Type$/ }),
    ).toBeInTheDocument();
    expect(
      within(header).getByRole("button", { name: /^Balance$/ }),
    ).toBeInTheDocument();
    // "Actions" is sr-only but still present in the accessible tree.
    expect(within(header).getByText(/^Actions$/)).toBeInTheDocument();
  });

  it("does not render the header when there are no accounts", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve({});
    }) as never);

    renderWithSWR(<AccountsPage />);
    await waitFor(() =>
      expect(screen.getByText(/No accounts yet/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("accounts-list-header")).toBeNull();
  });

  it("uses the same shared grid template on header and rows so columns align", async () => {
    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    // The header <tr> and each row <article> must carry the IDENTICAL
    // md:grid-cols-* template so the Account / Type / Balance / Actions
    // columns line up. A drift here is exactly the misalignment bug this
    // refactor fixes.
    const gridCols = (el: HTMLElement) =>
      Array.from(el.classList).find((c) => c.startsWith("md:grid-cols-"));

    const headerRow = within(
      screen.getByTestId("accounts-list-header"),
    ).getByRole("row");
    const nonDefaultRow = screen.getByTestId("account-row-10");
    const defaultRow = screen.getByTestId("account-row-20");

    expect(gridCols(headerRow)).toBeDefined();
    expect(gridCols(nonDefaultRow)).toBe(gridCols(headerRow));
    expect(gridCols(defaultRow)).toBe(gridCols(headerRow));
  });

  it("keeps the inline Edit button and exposes the rest via the overflow menu", async () => {
    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/ING Joint/)).toBeInTheDocument());

    // Edit stays inline on every row.
    expect(screen.getByRole("button", { name: /^Edit ING Joint$/ })).toBeInTheDocument();

    // Deactivate / Delete live behind the per-row "..." menu and are not
    // in the DOM until it is opened.
    expect(
      screen.queryByRole("menuitem", { name: /^Deactivate ING Joint$/ }),
    ).toBeNull();

    const actions = screen.getByTestId("account-row-actions-20");
    fireEvent.click(
      within(actions).getByRole("button", { name: /More actions for ING Joint/ }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: /^Deactivate ING Joint$/ }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("menuitem", { name: /^Delete ING Joint$/ }),
    ).toBeInTheDocument();
    // No "Set default" item on the default row.
    expect(
      screen.queryByRole("menuitem", { name: /Set ING Joint as default/ }),
    ).toBeNull();
  });

  it("offers Set default in the overflow menu only on a non-default active row", async () => {
    renderWithSWR(<AccountsPage />);
    await waitFor(() => expect(screen.getByText(/Amex Primary/)).toBeInTheDocument());

    const actions = screen.getByTestId("account-row-actions-10");
    fireEvent.click(
      within(actions).getByRole("button", { name: /More actions for Amex Primary/ }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("menuitem", { name: /^Set Amex Primary as default$/ }),
      ).toBeInTheDocument(),
    );
  });
});
