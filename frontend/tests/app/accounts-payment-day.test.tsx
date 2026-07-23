// CC Billing Cycle — Slice 2 (configurable payment day) frontend coverage.
//
// Targets the "Payment day" + "Payment month" inputs on /accounts: they
// appear only for credit_card accounts (same gating as close_day / Paid
// from), seed from the account's stored values, and submit payment_day +
// payment_day_relative_month on POST/PUT. The two controls map to their two
// columns INDEPENDENTLY (the resolver defaults each column separately):
//   - blank payment day  -> payment_day: null (resolver default day 1)
//   - "Month after close" (default) -> payment_day_relative_month: null
//     (resolver default 1 = month after close; keeps the column NULL-at-rest)
//   - "Same month as close"         -> payment_day_relative_month: 0
// Every combination is reachable, incl. "1st of same month" (day blank +
// same-month). Mirrors the mocking pattern in accounts-payment-source.test.tsx.

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
];

const CHECKING = {
  id: 10, name: "Primary", account_type_id: 1, account_type_name: "Checking",
  account_type_slug: "checking", balance: "150.00", currency: "EUR",
  is_active: true, is_default: true, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null, payment_day: null, payment_day_relative_month: null,
};
// CC seeded with a VALID same-month config: close on the 3rd, pay on the
// 5th of the SAME month (0). 5 > 3, so the payment-before-close guard is
// satisfied and the editor opens without the warning.
const CC = {
  id: 11, name: "Visa", account_type_id: 2, account_type_name: "Credit Card",
  account_type_slug: "credit_card", balance: "-50.00", currency: "EUR",
  is_active: true, is_default: false, close_day: 3,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null, payment_day: 5, payment_day_relative_month: 0,
};

function mockApi(accounts = [CHECKING, CC]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path === "/api/v1/accounts") return Promise.resolve(accounts);
    if (path.startsWith("/api/v1/accounts/") && path.endsWith("/cycle-payments")) {
      return Promise.resolve([]);
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

function findPutBody(accountId: number) {
  const putCall = vi
    .mocked(apiFetch)
    .mock.calls.find(
      ([path, init]) =>
        path === `/api/v1/accounts/${accountId}` && init?.method === "PUT",
    );
  expect(putCall).toBeTruthy();
  return JSON.parse(String(putCall![1]?.body));
}

describe("CC payment day — inline edit", () => {
  test("CC row shows Payment day + Payment month seeded from the account", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    const day = (await screen.findByLabelText("Payment day")) as HTMLInputElement;
    const month = (await screen.findByLabelText("Payment month")) as HTMLSelectElement;
    expect(day.value).toBe("5");
    expect(month.value).toBe("0"); // same month as close (relative_month = 0)
  });

  test("non-CC account edit row does not show the payment-day fields", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(10); // checking
    await screen.findByLabelText("Account type");
    expect(screen.queryByLabelText("Payment day")).toBeNull();
    expect(screen.queryByLabelText("Payment month")).toBeNull();
  });

  test("day + same-month select issue PUT with day and relative_month 0", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Payment day"), {
      target: { value: "20" },
    });
    // Seed already shows "Same month as close" (0); keep it.
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const body = findPutBody(11);
      expect(body.payment_day).toBe(20);
      expect(body.payment_day_relative_month).toBe(0);
    });
  });

  test("switching to 'Month after close' sends relative_month null (NULL-at-rest)", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Payment month"), {
      target: { value: "" }, // Month after close (the default option)
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const body = findPutBody(11);
      expect(body.payment_day).toBe(5); // unchanged
      expect(body.payment_day_relative_month).toBeNull();
    });
  });

  test("blank day + default month sends BOTH columns null (full resolver default)", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Payment day"), {
      target: { value: "" },
    });
    fireEvent.change(await screen.findByLabelText("Payment month"), {
      target: { value: "" }, // Month after close
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const body = findPutBody(11);
      expect(body.payment_day).toBeNull();
      expect(body.payment_day_relative_month).toBeNull();
    });
  });

  test("same-month payment on/before the close day warns and disables Save", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11); // seed close_day=3, valid (pay 5 > 3): no warning yet
    expect(screen.queryByRole("alert")).toBeNull();
    // Move the payment day to the close day (3): 3 <= 3 -> before/at close.
    fireEvent.change(await screen.findByLabelText("Payment day"), {
      target: { value: "3" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /must be after the close day/i,
    );
    const save = screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    // The edit row is a <div> (not a <form>), so the name input has a manual
    // Enter handler that calls handleSaveAcct directly, bypassing the
    // disabled button. handleSaveAcct must share the same guard: pressing
    // Enter while the config is invalid issues NO PUT.
    fireEvent.keyDown(screen.getByLabelText(/Account name/i), {
      key: "Enter",
      code: "Enter",
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(
      vi.mocked(apiFetch).mock.calls.find(
        ([path, init]) => path === "/api/v1/accounts/11" && init?.method === "PUT",
      ),
    ).toBeUndefined();
  });

  test("blank day + same-month is blocked (effective day 1 is on/before close)", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    await openEditRow(11);
    // Clear the day: effective payment_day defaults to 1, and 1 <= close_day
    // always, so "1st of the same month" is always before close.
    fireEvent.change(await screen.findByLabelText("Payment day"), {
      target: { value: "" },
    });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      (screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("CC payment day — create form", () => {
  test("fields appear only for credit_card and POST includes both columns", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "New CC" },
    });
    // Not present before a CC type is chosen.
    expect(screen.queryByLabelText(/Payment day/i)).toBeNull();
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Bill close day/i), {
      target: { value: "10" },
    });
    fireEvent.change(await screen.findByLabelText(/Payment day/i), {
      target: { value: "3" },
    });
    // Default Payment month is "Month after close" -> relative_month null.
    fireEvent.click(screen.getByRole("button", { name: /Create Account/i }));
    await waitFor(() => {
      const postCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts" && init?.method === "POST",
        );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(String(postCall![1]?.body));
      expect(body.payment_day).toBe(3);
      expect(body.payment_day_relative_month).toBeNull();
    });
  });

  test("create with 'Same month as close' sends relative_month 0", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "Same-month CC" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Bill close day/i), {
      target: { value: "10" },
    });
    fireEvent.change(await screen.findByLabelText(/Payment day/i), {
      target: { value: "25" },
    });
    fireEvent.change(await screen.findByLabelText("Payment month"), {
      target: { value: "0" }, // Same month as close
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
      expect(body.payment_day).toBe(25);
      expect(body.payment_day_relative_month).toBe(0);
    });
  });

  test("leaving payment day blank on create sends BOTH columns null", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "Blank CC" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Bill close day/i), {
      target: { value: "10" },
    });
    // Payment day left blank.
    fireEvent.click(screen.getByRole("button", { name: /Create Account/i }));
    await waitFor(() => {
      const postCall = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts" && init?.method === "POST",
        );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(String(postCall![1]?.body));
      expect(body.payment_day).toBeNull();
      expect(body.payment_day_relative_month).toBeNull();
    });
  });

  test("same-month payment before close warns and disables Create", async () => {
    mockApi();
    renderWithSWR(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "Before-close CC" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "2" } });
    fireEvent.change(await screen.findByLabelText(/Bill close day/i), {
      target: { value: "25" },
    });
    fireEvent.change(await screen.findByLabelText(/Payment day/i), {
      target: { value: "5" }, // 5 <= 25
    });
    fireEvent.change(await screen.findByLabelText("Payment month"), {
      target: { value: "0" }, // same month
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /must be after the close day/i,
    );
    expect(
      (screen.getByRole("button", { name: /Create Account/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });
});
