/**
 * TBD-600. A recurring transfer on the recurring page.
 *
 *  T1  The row reads "From -> To" and its amount carries no income/expense sign.
 *  T2  Edit series shows the destination account for a transfer only, lists
 *      transfer ("both") categories only, and PUTs only `to_account_id`.
 *  T3  The destination cannot equal the source: Save stays disabled.
 *  T4  Edit next on a transfer applies to this occurrence only: no scope
 *      choice, and the PUT sends series_scope "this".
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { formatMoney } from "@/lib/format";
import type { Account, Category, RecurringTransaction } from "@/lib/types";
import { renderWithSWR } from "../utils/render-with-swr";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/recurring",
  useSearchParams: () => new URLSearchParams(),
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
  subscription_status: null, subscription_plan: null, trial_end: null,
  allow_manual_balance_adjustment: false,
};

function rec(over: Partial<RecurringTransaction>): RecurringTransaction {
  return {
    id: 1, account_id: 1, account_name: "Checking", category_id: 10,
    category_name: "Bills", description: "x", amount: "10.00",
    type: "expense", frequency: "monthly", next_due_date: "2026-10-01",
    auto_settle: false, is_active: true, occurrence_count: null,
    occurrences_elapsed: 0, ...over,
  };
}

const RENT = rec({ id: 41, description: "Rent", amount: "1200.00" });
const SAVE = rec({
  id: 50, description: "To savings", amount: "300.00", type: "transfer",
  to_account_id: 2, to_account_name: "Savings", category_id: 30, category_name: "Transfers",
});

function acct(over: Partial<Account>): Account {
  return {
    id: 1, name: "Checking", account_type_id: 1, account_type_name: "Checking",
    account_type_slug: "checking", balance: 0, currency: "EUR", is_active: true,
    close_day: null, is_default: true, ...over,
  } as Account;
}
const ACCOUNTS: Account[] = [
  acct({ id: 1, name: "Checking" }),
  acct({ id: 2, name: "Savings", is_default: false }),
  acct({ id: 3, name: "Pension", is_default: false }),
];

function cat(id: number, name: string, type: Category["type"]): Category {
  return {
    id, name, type, parent_id: null, parent_name: null, description: null,
    slug: null, is_system: false, transaction_count: 0,
  };
}
const CATEGORIES: Category[] = [cat(10, "Bills", "expense"), cat(30, "Transfers", "both")];

function mockApi(handlers: Record<string, (init?: RequestInit) => unknown> = {}) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    if (key === "GET /api/v1/recurring") return Promise.resolve([RENT, SAVE]);
    if (key === "GET /api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (key === "GET /api/v1/categories") return Promise.resolve(CATEGORIES);
    const h = handlers[key];
    if (!h) return Promise.resolve(key.startsWith("GET ") ? [] : undefined);
    return Promise.resolve().then(() => h(init));
  }) as never);
}

const puts = () =>
  vi.mocked(apiFetch).mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([url, init]) => ({ url, body: JSON.parse(String((init as RequestInit).body)) }));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

async function openDialog(label: string) {
  const section = await screen.findByTestId("recurring-active-table");
  const table = within(section).getByRole("table");
  fireEvent.click(within(table).getByLabelText(label));
  const dialog = await screen.findByRole("dialog");
  return dialog;
}

describe("recurring page: transfer series (TBD-600)", () => {
  it("T1: the row reads From -> To and the amount has no sign", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const table = within(await screen.findByTestId("recurring-active-table")).getByRole("table");
    const row = within(table).getByText("To savings").closest("tr")!;
    expect(within(row).getByText("Checking → Savings")).toBeInTheDocument();
    expect(within(row).getByText(formatMoney(300))).toBeInTheDocument();
    expect(row.textContent).not.toContain(`-${formatMoney(300)}`);
  });

  it("T2: Edit series shows the destination for a transfer and PUTs only it", async () => {
    mockApi({ "PUT /api/v1/recurring/50": () => ({ ...SAVE, to_account_id: 3 }) });
    renderWithSWR(<RecurringPage />);
    const dialog = await openDialog("Edit series: To savings");
    await within(dialog).findAllByRole("option", { name: "Pension" });
    const to = within(dialog).getByLabelText("To account") as HTMLSelectElement;
    expect(to.value).toBe("2");
    expect(within(dialog).getByLabelText("From account")).toBeInTheDocument();
    fireEvent.focus(within(dialog).getByLabelText("Category"));
    const categories = within(within(dialog).getByRole("listbox"))
      .getAllByRole("option").map((o) => o.textContent);
    expect(categories).toContain("Transfers");
    expect(categories).not.toContain("Bills");

    fireEvent.change(to, { target: { value: "3" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0]).toEqual({ url: "/api/v1/recurring/50", body: { to_account_id: 3 } });
  });

  it("T2: an expense series has no destination account", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await openDialog("Edit series: Rent");
    await within(dialog).findByRole("option", { name: "Savings" });
    expect(within(dialog).queryByLabelText("To account")).toBeNull();
    expect(within(dialog).getByLabelText("Account")).toBeInTheDocument();
  });

  it("T3: the destination cannot be the source", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await openDialog("Edit series: To savings");
    await within(dialog).findAllByRole("option", { name: "Pension" });
    fireEvent.change(within(dialog).getByLabelText("To account"), { target: { value: "1" } });
    expect(within(dialog).getByText("Choose an account other than the source.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("T4: Edit next on a transfer is this occurrence only", async () => {
    mockApi({
      "POST /api/v1/recurring/50/materialise-next": () => ({ id: 900 }),
      "PUT /api/v1/transactions/900": () => ({ id: 900 }),
    });
    renderWithSWR(<RecurringPage />);
    const dialog = await openDialog("Edit next amount: To savings");
    expect(within(dialog).queryByLabelText(/This and future occurrences/)).toBeNull();
    fireEvent.change(within(dialog).getByLabelText("Amount for 2026-10-01"), {
      target: { value: "350" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save amount" }));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0]).toEqual({
      url: "/api/v1/transactions/900", body: { amount: "350", series_scope: "this" },
    });
  });
});
