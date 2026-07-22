// Payment Source Foundation — frontend coverage.
//
// Targets the "Paid from" picker on /accounts: it appears only for
// credit_card accounts, lists same-org checking/savings/cash accounts
// (never credit_card / investment / self), submits payment_source_account_id
// on the PUT/POST body, and renders a read-only "paid from <name>" detail
// line. Mirrors the mocking pattern in accounts-edit-type.test.tsx.

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
  { id: 1, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
  { id: 2, name: "Credit Card", slug: "credit_card", is_system: true, account_count: 1 },
  { id: 3, name: "Savings", slug: "savings", is_system: true, account_count: 1 },
  { id: 4, name: "Investment", slug: "investment", is_system: true, account_count: 1 },
];

const CHECKING = {
  id: 10, name: "Primary", account_type_id: 1, account_type_name: "Checking",
  account_type_slug: "checking", balance: "150.00", currency: "EUR",
  is_active: true, is_default: true, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null,
};
const SAVINGS = {
  id: 12, name: "Rainy Day", account_type_id: 3, account_type_name: "Savings",
  account_type_slug: "savings", balance: "500.00", currency: "EUR",
  is_active: true, is_default: false, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null,
};
const INVESTMENT = {
  id: 13, name: "Brokerage", account_type_id: 4, account_type_name: "Investment",
  account_type_slug: "investment", balance: "9000.00", currency: "EUR",
  is_active: true, is_default: false, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null,
};
const CC = {
  id: 11, name: "Visa", account_type_id: 2, account_type_name: "Credit Card",
  account_type_slug: "credit_card", balance: "-50.00", currency: "EUR",
  is_active: true, is_default: false, close_day: 15,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: 10, // paid from Primary
};

function mockApi(accounts = [CHECKING, SAVINGS, INVESTMENT, CC]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path === "/api/v1/accounts") return Promise.resolve(accounts);
    if (path.startsWith("/api/v1/accounts/") && path.endsWith("/reconcile")) {
      return Promise.resolve({});
    }
    if (path.startsWith("/api/v1/transactions")) {
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    }
    return Promise.resolve([]);
  });
}

function setupAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    refresh: vi.fn(),
    logout: vi.fn(),
    login: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.clearAllMocks();
  setupAuth();
});

async function openEditRow(accountId: number) {
  const row = await screen.findByTestId(`account-row-${accountId}`);
  fireEvent.click(within(row).getByRole("button", { name: /^Edit / }));
}

describe("Payment Source — inline edit picker", () => {
  test("CC row shows the Paid from picker seeded with the current source", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const picker = (await screen.findByLabelText("Paid from")) as HTMLSelectElement;
    expect(picker.value).toBe("10");
  });

  test("picker lists only asset accounts, excluding the CC itself and investment", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const picker = (await screen.findByLabelText("Paid from")) as HTMLSelectElement;
    const labels = within(picker)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).textContent);
    expect(labels).toContain("(none)");
    expect(labels).toContain("Primary");
    expect(labels).toContain("Rainy Day");
    expect(labels).not.toContain("Brokerage"); // investment excluded
    expect(labels).not.toContain("Visa"); // self excluded
  });

  test("non-CC account edit row does not show the Paid from picker", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(10); // checking
    // Give the row a beat to render its fields.
    await screen.findByLabelText("Account type");
    expect(screen.queryByLabelText("Paid from")).toBeNull();
  });

  test("selecting a new source issues PUT with payment_source_account_id", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Paid from"), {
      target: { value: "12" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const putCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts/11" && init?.method === "PUT",
        );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String(putCall![1]?.body));
      expect(body.payment_source_account_id).toBe(12);
    });
  });

  test("clearing the source to (none) sends payment_source_account_id: null", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Paid from"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const putCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts/11" && init?.method === "PUT",
        );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String(putCall![1]?.body));
      expect(body.payment_source_account_id).toBeNull();
    });
  });
});

describe("Payment Source — detail line + create form", () => {
  test("CC row renders a read-only 'paid from <name>' detail", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/paid from Primary/i)).toBeTruthy();
  });

  test("create form surfaces the Paid from picker only for credit_card and POSTs it", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "New CC" },
    });
    // No picker before a CC type is chosen.
    expect(screen.queryByLabelText("Paid from")).toBeNull();
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Bill close day/i), {
      target: { value: "10" },
    });
    fireEvent.change(await screen.findByLabelText("Paid from"), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create Account/i }));
    await waitFor(() => {
      const postCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts" && init?.method === "POST",
        );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(String(postCall![1]?.body));
      expect(body.payment_source_account_id).toBe(10);
    });
  });
});
