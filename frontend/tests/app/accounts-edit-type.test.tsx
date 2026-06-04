// Edit Account Type — frontend coverage (spec § 8.2).
//
// Targets the inline-edit row on /accounts: type select, conditional
// close-day input, confirm modal, error handling. Mirrors the
// mocking pattern in accounts-adjust-balance-button.test.tsx.

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
  { id: 1, name: "Checking", slug: "checking", is_system: true, account_count: 1 },
  { id: 2, name: "Credit Card", slug: "credit_card", is_system: true, account_count: 1 },
  { id: 3, name: "Savings", slug: "savings", is_system: true, account_count: 0 },
];

const CHECKING_ACCT = {
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
  opening_balance: "0.00",
  opening_balance_date: "2026-01-01",
};

const CC_ACCT = {
  id: 11,
  name: "Visa",
  account_type_id: 2,
  account_type_name: "Credit Card",
  account_type_slug: "credit_card",
  balance: "-50.00",
  currency: "EUR",
  is_active: true,
  is_default: false,
  close_day: 15,
  opening_balance: "0.00",
  opening_balance_date: "2026-01-01",
};

function mockApi(accounts = [CHECKING_ACCT, CC_ACCT]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path === "/api/v1/accounts") return Promise.resolve(accounts);
    if (path.startsWith("/api/v1/accounts/") && path.endsWith("/reconcile")) {
      return Promise.resolve({});
    }
    if (path.startsWith("/api/v1/transactions")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
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

describe("Edit Account Type — inline edit row", () => {
  test("renders type select pre-filled with current type", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    const select = await screen.findByLabelText("Account type");
    expect((select as HTMLSelectElement).value).toBe("1");
  });

  test("changing to Credit Card reveals close-day input", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    expect(screen.queryByLabelText("Close day")).toBeNull();
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    expect(await screen.findByLabelText("Close day")).toBeTruthy();
  });

  test("changing away from Credit Card hides close-day input and clears its local value", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(11); // CC account
    const closeDay = (await screen.findByLabelText("Close day")) as HTMLInputElement;
    fireEvent.change(closeDay, { target: { value: "20" } });
    expect(closeDay.value).toBe("20");

    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "1" },
    });
    // Hidden after switching off CC.
    expect(screen.queryByLabelText("Close day")).toBeNull();

    // Switching back to CC must show an EMPTY input (cleared local state).
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    const cdAgain = (await screen.findByLabelText("Close day")) as HTMLInputElement;
    expect(cdAgain.value).toBe("");
  });

  test("clicking Save with type change shows the confirmation dialog", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText("Close day"), {
      target: { value: "15" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(await screen.findByText(/Change account type\?/i)).toBeTruthy();
  });

  test("clicking Cancel in the dialog leaves the row in edit mode and sends no request", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText("Close day"), {
      target: { value: "15" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^Cancel$/ }));

    await waitFor(() => {
      expect(screen.queryByText(/Change account type\?/i)).toBeNull();
    });
    // No PUT issued.
    const putCalls = vi
      .mocked(apiFetch)
      .mock.calls.filter(
        ([, init]) => init?.method === "PUT" && !!init.body,
      );
    expect(putCalls).toHaveLength(0);
  });

  test("clicking Change type in the dialog issues PUT with {account_type_id, close_day}", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText("Close day"), {
      target: { value: "20" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Change type/i }));

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([path, init]) =>
          typeof path === "string"
          && path === "/api/v1/accounts/10"
          && init?.method === "PUT",
      );
      expect(putCall).toBeTruthy();
      const body = JSON.parse(String(putCall![1]?.body));
      expect(body.account_type_id).toBe(2);
      expect(body.close_day).toBe(20);
    });
  });

  test("name-only edit does not show the confirmation dialog", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(10);
    fireEvent.change(await screen.findByLabelText("Account name"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([path, init]) => path === "/api/v1/accounts/10" && init?.method === "PUT",
      );
      expect(putCall).toBeTruthy();
    });
    expect(screen.queryByText(/Change account type\?/i)).toBeNull();
  });

  test("close-day-only edit on a CC account does not show the confirmation dialog", async () => {
    mockApi();
    render(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Close day"), {
      target: { value: "20" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls;
      const putCall = calls.find(
        ([path, init]) => path === "/api/v1/accounts/11" && init?.method === "PUT",
      );
      expect(putCall).toBeTruthy();
    });
    expect(screen.queryByText(/Change account type\?/i)).toBeNull();
  });

  test("error from server is surfaced via the page error banner", async () => {
    mockApi();
    // Override PUT to throw.
    vi.mocked(apiFetch).mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
      if (path === "/api/v1/accounts") return Promise.resolve([CHECKING_ACCT, CC_ACCT]);
      if (path.startsWith("/api/v1/transactions")) return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      if (init?.method === "PUT") {
        return Promise.reject(new Error("close_day is only allowed on credit_card accounts"));
      }
      return Promise.resolve([]);
    });

    render(<AccountsPage />);
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Change type/i }));
    expect(
      await screen.findByText(/close_day is only allowed on credit_card accounts/i),
    ).toBeTruthy();
  });

  test("dialog message mentions clearing close day when leaving CC and Pending default when entering CC", async () => {
    mockApi();
    render(<AccountsPage />);
    // Entering CC.
    await openEditRow(10);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "2" },
    });
    fireEvent.change(await screen.findByLabelText("Close day"), {
      target: { value: "15" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    const enterMsg = await screen.findByText(/closing day/i);
    expect(enterMsg.textContent).toMatch(/Pending/i);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /^Cancel$/ }));

    // Leaving CC.
    await openEditRow(11);
    fireEvent.change(await screen.findByLabelText("Account type"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    const leaveMsg = await screen.findByText(/clear the closing day/i);
    expect(leaveMsg).toBeTruthy();
  });
});

describe("Edit Account Type — create form close_day required (§ 5.6)", () => {
  test("create form marks close_day as required when type is credit_card", async () => {
    mockApi();
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/^Type$/), {
      target: { value: "2" },
    });
    const closeDay = (await screen.findByLabelText(/Bill close day/i)) as HTMLInputElement;
    expect(closeDay.required).toBe(true);
  });

  test("create form blocks submission when close_day is empty and type is credit_card", async () => {
    mockApi();
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Account/i }));
    fireEvent.change(await screen.findByLabelText(/Account name/i), {
      target: { value: "New CC" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), {
      target: { value: "2" },
    });
    const closeDay = (await screen.findByLabelText(/Bill close day/i)) as HTMLInputElement;
    expect(closeDay.value).toBe("");
    // Submit attempt — the HTML5 required attribute prevents the form
    // from POSTing. We assert by inspecting checkValidity().
    const form = closeDay.closest("form");
    expect(form?.checkValidity()).toBe(false);
    // Sanity: no POST was issued before the click.
    const postCalls = vi
      .mocked(apiFetch)
      .mock.calls.filter(([, init]) => init?.method === "POST");
    expect(postCalls).toHaveLength(0);
  });
});
