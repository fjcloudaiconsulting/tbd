// Credit Card Model V1 (Slice 1) — frontend coverage for Task 7.
//
// Targets the CC-gated form fields on /accounts: credit_limit, apr,
// payment_strategy, fixed_payment_amount. Mirrors the mocking pattern in
// accounts-payment-source.test.tsx (harness copied verbatim below).

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
  credit_limit: "2000.00" as string | null, apr: "19.99", payment_strategy: "full_balance",
  fixed_payment_amount: null,
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
    if (path.match(/\/api\/v1\/accounts\/\d+\/cycle-payments$/)) {
      return Promise.resolve([
        { year: 2026, month: 8, close_date: "2026-08-15", due_date: "2026-09-01", amount: null },
        { year: 2026, month: 9, close_date: "2026-09-15", due_date: "2026-10-01", amount: "120.00" },
        { year: 2026, month: 10, close_date: "2026-10-15", due_date: "2026-11-01", amount: null },
      ]);
    }
    if (path.match(/\/api\/v1\/accounts\/\d+\/cycle-payments\/\d+\/\d+$/)) {
      return Promise.resolve({});
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
  // Entering edit mode on a CC row (Task 6) fires the gated
  // upcoming-cycle-payments fetch. Flush it inside `act` here so its
  // setState settles before the test continues, instead of resolving
  // later and tripping "not wrapped in act(...)" warnings (Task 7 review
  // finding #2). Harmless no-op for non-CC rows, which never start that
  // fetch.
  await act(async () => {
    fireEvent.click(within(row).getByRole("button", { name: /^Edit / }));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("CC Model — form fields", () => {
  test("edit row shows credit limit, APR and strategy for a CC account", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(await screen.findByLabelText(/Credit limit/i)).toBeTruthy();
    expect(screen.getByLabelText(/APR/i)).toBeTruthy();
    expect(screen.getByLabelText(/Payment strategy/i)).toBeTruthy();
  });

  test("fixed payment amount appears only under the fixed_amount strategy", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(screen.queryByLabelText(/Fixed payment amount/i)).toBeNull();
    fireEvent.change(await screen.findByLabelText(/Payment strategy/i), {
      target: { value: "fixed_amount" },
    });
    expect(await screen.findByLabelText(/Fixed payment amount/i)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Payment strategy/i), {
      target: { value: "full_balance" },
    });
    expect(screen.queryByLabelText(/Fixed payment amount/i)).toBeNull();
  });

  test("PUT body carries the four CC fields for a CC account", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText(/Credit limit/i), {
      target: { value: "5000" },
    });
    fireEvent.change(screen.getByLabelText(/APR/i), { target: { value: "21.5" } });
    fireEvent.change(screen.getByLabelText(/Payment strategy/i), {
      target: { value: "fixed_amount" },
    });
    fireEvent.change(await screen.findByLabelText(/Fixed payment amount/i), {
      target: { value: "150" },
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
      expect(body.credit_limit).toBe("5000");
      expect(body.apr).toBe("21.5");
      expect(body.payment_strategy).toBe("fixed_amount");
      expect(body.fixed_payment_amount).toBe("150");
    });
  });

  test("non-CC edit row shows none of the CC fields", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(10);
    await screen.findByLabelText("Account type");
    expect(screen.queryByLabelText(/Credit limit/i)).toBeNull();
    expect(screen.queryByLabelText(/Payment strategy/i)).toBeNull();
  });
});

describe("CC Model — utilization subline", () => {
  function ccWith(balance: string, credit_limit: string | null) {
    return { ...CC, balance, credit_limit };
  }

  test("within-limit shows 'Using n% of limit · <available> <ccy> left'", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-500.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Using 25% of limit · 1,500\.00 EUR left/)).toBeTruthy();
  });

  test("zero outstanding shows the full-limit copy", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("0.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/0% used · full limit available/)).toBeTruthy();
  });

  test("over-limit shows the '<over> <ccy> over' copy (uncapped %)", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-2500.00", "2000.00")]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).getByText(/Using 125% of limit · 500\.00 EUR over/)).toBeTruthy();
  });

  test("no subline when credit_limit is null or zero", async () => {
    mockApi([CHECKING, SAVINGS, ccWith("-500.00", null)]);
    renderWithSWR(<AccountsPage />);
    const row = await screen.findByTestId("account-row-11");
    expect(within(row).queryByText(/of limit/)).toBeNull();
    expect(within(row).queryByText(/full limit available/)).toBeNull();
  });
});

describe("CC Model — upcoming payments mini-list", () => {
  it("shows Upcoming payments for a full_balance CC and hides removed strategy options", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    expect(screen.getByText("Upcoming payments")).toBeInTheDocument();
    expect(
      screen.getByText(/Paying the full balance by default\. Enter a different amount/),
    ).toBeInTheDocument();
    const select = screen.getByLabelText("Payment strategy") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).not.toContain("minimum_only");
    expect(optionValues).not.toContain("custom_per_period");
    expect(optionValues).toEqual(["", "full_balance", "fixed_amount"]);
  });

  test("keeps showing Upcoming payments when switching away from full_balance", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11); // CC seeded with payment_strategy=full_balance
    expect(await screen.findByText(/Upcoming payments/i)).toBeTruthy();
    expect(screen.getByText(/Closes 2026-08-15 · due 2026-09-01/)).toBeTruthy();
    fireEvent.change(await screen.findByLabelText(/Payment strategy/i), {
      target: { value: "fixed_amount" },
    });
    expect(screen.getByText(/Upcoming payments/i)).toBeTruthy();
  });

  test("empty amount persists via DELETE, filled via PUT", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const firstInput = await screen.findByLabelText(/Planned payment for 2026-08/i);
    fireEvent.change(firstInput, { target: { value: "90" } });
    fireEvent.blur(firstInput);
    await waitFor(() => {
      const put = vi.mocked(apiFetch).mock.calls.find(
        ([p, init]) =>
          p === "/api/v1/accounts/11/cycle-payments/2026/8" && init?.method === "PUT",
      );
      expect(put).toBeTruthy();
      expect(JSON.parse(String(put![1]?.body)).amount).toBe("90");
    });
  });

  test("clearing a saved amount deletes it", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const clears = await screen.findAllByRole("button", { name: /^Clear$/ });
    fireEvent.click(clears[0]);
    await waitFor(() => {
      const del = vi.mocked(apiFetch).mock.calls.find(
        ([p, init]) =>
          p === "/api/v1/accounts/11/cycle-payments/2026/9" && init?.method === "DELETE",
      );
      expect(del).toBeTruthy();
    });
  });
});
