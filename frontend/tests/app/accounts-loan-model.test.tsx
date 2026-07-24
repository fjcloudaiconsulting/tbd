// Loan Account Type V1 (Slice 1) — frontend coverage.
//
// Targets the loan-gated form fields on /accounts (principal_amount,
// interest_rate_apr, term_months, origination_date, first_payment_date), the
// shared "Paid from" picker de-gate, and the read-only loan subline. Mirrors
// the mocking harness in accounts-cc-model.test.tsx.

import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
  { id: 2, name: "Loan", slug: "loan", is_system: true, account_count: 1 },
  { id: 3, name: "Savings", slug: "savings", is_system: true, account_count: 1 },
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
const LOAN = {
  id: 11, name: "Car Loan", account_type_id: 2, account_type_name: "Loan",
  account_type_slug: "loan", balance: "-18000.00", currency: "EUR",
  is_active: true, is_default: false, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: 10, // paid from Primary
  principal_amount: "20000.00" as string | null,
  interest_rate_apr: "6.50" as string | null,
  term_months: 60 as number | null,
  origination_date: "2026-01-01" as string | null,
  first_payment_date: "2026-02-01" as string | null,
  loan: {
    expected_monthly_payment: "391.32",
    maturation_date: "2031-01-01",
    total_interest: "3479.20",
    projected_payoff_date: "2030-11-01",
    projected_payoff_months: 58,
    status: "on_track" as "on_track" | "paid_off" | "interest_only",
  } as Record<string, unknown> | null,
};

function mockApi(accounts = [CHECKING, SAVINGS, LOAN]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path === "/api/v1/accounts") return Promise.resolve(accounts);
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
  await act(async () => {
    fireEvent.click(within(row).getByRole("button", { name: /^Edit / }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("Loan Model — form fields", () => {
  test("edit row shows the five loan fields and the Paid from picker", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(await screen.findByLabelText(/Principal amount/i)).toBeTruthy();
    expect(screen.getByLabelText(/Interest rate/i)).toBeTruthy();
    expect(screen.getByLabelText(/Term \(months\)/i)).toBeTruthy();
    expect(screen.getByLabelText(/Origination date/i)).toBeTruthy();
    expect(screen.getByLabelText(/First payment date/i)).toBeTruthy();
    expect(screen.getByLabelText(/Paid from/i)).toBeTruthy();
  });

  test("PUT body carries the five loan fields and paid-from for a loan", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText(/Principal amount/i), {
      target: { value: "25000" },
    });
    fireEvent.change(screen.getByLabelText(/Interest rate/i), { target: { value: "5.25" } });
    fireEvent.change(screen.getByLabelText(/Term \(months\)/i), { target: { value: "72" } });
    fireEvent.change(screen.getByLabelText(/Origination date/i), {
      target: { value: "2026-03-01" },
    });
    fireEvent.change(screen.getByLabelText(/First payment date/i), {
      target: { value: "2026-04-01" },
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
      expect(body.principal_amount).toBe("25000");
      expect(body.interest_rate_apr).toBe("5.25");
      expect(body.term_months).toBe(72);
      expect(body.origination_date).toBe("2026-03-01");
      expect(body.first_payment_date).toBe("2026-04-01");
      expect(body.payment_source_account_id).toBe(10);
    });
  });

  test("non-loan edit row shows none of the loan fields", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(10);
    await screen.findByLabelText("Account type");
    expect(screen.queryByLabelText(/Principal amount/i)).toBeNull();
    expect(screen.queryByLabelText(/Term \(months\)/i)).toBeNull();
  });
});

describe("Loan Model — read-only subline", () => {
  function loanWith(overrides: Record<string, unknown>) {
    return { ...LOAN, ...overrides };
  }

  test("on_track loan shows monthly payment, matures, total interest and payoff date", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Monthly payment 391\.32 EUR/)).toBeTruthy();
    expect(within(row).getByText(/Matures 2031-01-01/)).toBeTruthy();
    expect(within(row).getByText(/Total interest \(full term\) 3,479\.20 EUR/)).toBeTruthy();
    expect(within(row).getByText(/On track to pay off 2030-11-01/)).toBeTruthy();
  });

  test("interest_only shows a quiet 'Not on track to pay off' with no date", async () => {
    mockApi([
      CHECKING,
      loanWith({
        loan: {
          expected_monthly_payment: "100.00",
          maturation_date: "2031-01-01",
          total_interest: "6000.00",
          projected_payoff_date: null,
          projected_payoff_months: null,
          status: "interest_only",
        },
      }),
    ]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Not on track to pay off/)).toBeTruthy();
    expect(within(row).queryByText(/On track to pay off/)).toBeNull();
    // Never surfaces the raw "interest-only" product phrasing.
    expect(within(row).queryByText(/interest.only/i)).toBeNull();
  });

  test("paid_off shows 'Paid off'", async () => {
    mockApi([
      CHECKING,
      loanWith({
        balance: "0.00",
        loan: {
          expected_monthly_payment: "391.32",
          maturation_date: "2031-01-01",
          total_interest: "3479.20",
          projected_payoff_date: "2026-07-24",
          projected_payoff_months: 0,
          status: "paid_off",
        },
      }),
    ]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/^Paid off$/)).toBeTruthy();
  });

  test("no loan subline when the loan metrics object is null", async () => {
    mockApi([CHECKING, loanWith({ loan: null })]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).queryByText(/Monthly payment/)).toBeNull();
    expect(within(row).queryByText(/Matures/)).toBeNull();
  });
});
