/**
 * TBD-762. "Edit series" on the recurring page: a dialog that edits the whole
 * template and PUTs only the fields the user changed.
 *
 *  E1  An entry point per row in BOTH trees (desktop table, mobile card), in
 *      the Active AND the Paused table, and for a finished instalment series.
 *  E2  Every field opens prefilled from the row; the type is not editable.
 *  E3  Save is disabled until something changes; the PUT carries exactly the
 *      changed keys. Kills: a full-form body (which would re-send the date and
 *      trip the server's next-due guard on an unrelated edit).
 *  E4  The category picker lists the series' type and "both" only.
 *  E5  An instalment series shows "N of M done"; Never sends occurrence_count
 *      null (an omitted key would leave the count alone).
 *  E6  A different-currency account warns, in one line.
 *  E7  A refusal (the next-due guard's 409) is shown in the dialog, which
 *      stays open with the user's input.
 *  E8  A successful save closes the dialog, says so and reloads the list.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
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
    type: "expense", frequency: "monthly", next_due_date: "2026-09-01",
    auto_settle: false, is_active: true, occurrence_count: null,
    occurrences_elapsed: 0, ...over,
  };
}

const RENT = rec({ id: 41, description: "Rent", amount: "1200.00", next_due_date: "2026-10-01" });
const SOFA = rec({
  id: 42, description: "Sofa", amount: "250.00", next_due_date: "2026-08-15",
  occurrence_count: 12, occurrences_elapsed: 3, auto_settle: true,
});
const LAPTOP_DONE = rec({
  id: 43, description: "Laptop", amount: "99.00", next_due_date: "2026-07-01",
  occurrence_count: 12, occurrences_elapsed: 12,
});
const SALARY = rec({
  id: 45, description: "Salary", amount: "3000.00", type: "income",
  category_id: 20, category_name: "Wages", next_due_date: "2026-09-25",
});
const PAUSED = rec({ id: 44, description: "Gym", amount: "30.00", is_active: false });
const ROWS = [RENT, SOFA, LAPTOP_DONE, SALARY, PAUSED];

function acct(over: Partial<Account>): Account {
  return {
    id: 1, name: "Checking", account_type_id: 1, account_type_name: "Checking",
    account_type_slug: "checking", balance: 0, currency: "EUR", is_active: true,
    close_day: null, is_default: true, ...over,
  } as Account;
}
// Two currencies, as a legacy multi-currency org has: the org currency is then
// unknown on the client, and the series' own account is the reference.
const ACCOUNTS: Account[] = [
  acct({ id: 1, name: "Checking" }),
  acct({ id: 2, name: "Savings", is_default: false }),
  acct({ id: 3, name: "Travel card", currency: "USD", is_default: false }),
  acct({ id: 4, name: "Old account", is_active: false, is_default: false }),
];

function cat(id: number, name: string, type: Category["type"]): Category {
  return {
    id, name, type, parent_id: null, parent_name: null, description: null,
    slug: null, is_system: false, transaction_count: 0,
  };
}
const CATEGORIES: Category[] = [
  cat(10, "Bills", "expense"),
  cat(11, "Groceries", "expense"),
  cat(20, "Wages", "income"),
  cat(21, "Refunds", "income"),
  cat(30, "Transfers", "both"),
];

type Handler = (init?: RequestInit) => unknown;

function mockApi(handlers: Record<string, Handler> = {}) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    if (key === "GET /api/v1/recurring") return Promise.resolve(ROWS);
    if (key === "GET /api/v1/accounts") return Promise.resolve(ACCOUNTS);
    if (key === "GET /api/v1/categories") return Promise.resolve(CATEGORIES);
    const h = handlers[key];
    // Anything else the shell asks for resolves empty; a PUT nobody expected
    // still fails the test through `puts()` below.
    if (!h) return Promise.resolve(key.startsWith("GET ") ? [] : undefined);
    return Promise.resolve().then(() => h(init));
  }) as never);
}

const puts = () =>
  vi.mocked(apiFetch).mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "PUT")
    .map(([url, init]) => ({ url, body: JSON.parse(String((init as RequestInit).body)) }));

const listLoads = () =>
  vi.mocked(apiFetch).mock.calls.filter(
    ([url, init]) => url === "/api/v1/recurring" && !(init as RequestInit | undefined)?.method,
  ).length;

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

async function trees(testId: string, label: string) {
  const section = await screen.findByTestId(testId);
  const table = within(section).getByRole("table");
  const all = within(section).queryAllByLabelText(label);
  return {
    desktop: all.filter((el) => table.contains(el)),
    mobile: all.filter((el) => !table.contains(el)),
  };
}

/** Opens the dialog and waits for the account list, so every field is live. */
async function open(description: string, { table = "recurring-active-table", tree = "desktop" } = {}) {
  const t = await trees(table, `Edit series: ${description}`);
  fireEvent.click((tree === "desktop" ? t.desktop : t.mobile)[0]);
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByRole("option", { name: "Savings" });
  return dialog;
}

const field = (dialog: HTMLElement, label: string) =>
  within(dialog).getByLabelText(label) as HTMLInputElement;
// The category picker's own list: the account and frequency <select>s have
// options too.
const categoryOptions = (dialog: HTMLElement) =>
  within(within(dialog).getByRole("listbox")).getAllByRole("option").map((o) => o.textContent);
const save = (dialog: HTMLElement) =>
  within(dialog).getByRole("button", { name: "Save changes" });

describe("recurring page: edit series (TBD-762)", () => {
  it("E1: an entry point in both trees, Active and Paused, finished series included", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    for (const name of ["Rent", "Laptop", "Salary"]) {
      const t = await trees("recurring-active-table", `Edit series: ${name}`);
      expect(t.desktop, `${name} desktop`).toHaveLength(1);
      expect(t.mobile, `${name} mobile`).toHaveLength(1);
    }
    const paused = await trees("recurring-paused-table", "Edit series: Gym");
    expect(paused.desktop).toHaveLength(1);
    expect(paused.mobile).toHaveLength(1);
  });

  it("E1: the mobile card and the Paused table open the same dialog", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    let dialog = await open("Rent", { tree: "mobile" });
    expect(field(dialog, "Name").value).toBe("Rent");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    dialog = await open("Gym", { table: "recurring-paused-table" });
    expect(field(dialog, "Name").value).toBe("Gym");
  });

  it("E1: closing the dialog hands focus back to the row's button", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const { desktop } = await trees("recurring-active-table", "Edit series: Rent");
    desktop[0].focus();
    fireEvent.click(desktop[0]);
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("option", { name: "Savings" });
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(desktop[0]);
  });

  it("E2: every field opens prefilled, and the type is not offered", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    expect(within(dialog).getByText("Edit Series")).toBeInTheDocument();
    expect(field(dialog, "Name").value).toBe("Rent");
    expect(field(dialog, "Amount").value).toBe("1200.00");
    expect(field(dialog, "Category").value).toBe("Bills");
    expect(field(dialog, "Account").value).toBe("1");
    expect(field(dialog, "Frequency").value).toBe("monthly");
    expect(field(dialog, "Next due date").value).toBe("2026-10-01");
    expect(field(dialog, "Never").checked).toBe(true);
    expect(field(dialog, "Number of occurrences")).toBeDisabled();
    expect(field(dialog, "Auto-settle").checked).toBe(false);

    expect(within(dialog).queryByLabelText(/^type$/i)).toBeNull();
    expect(within(dialog).queryByRole("radio", { name: /income|expense/i })).toBeNull();
    expect(within(dialog).queryByRole("option", { name: /^(income|expense)$/i })).toBeNull();
    // Inactive accounts are not offered, except the series' own.
    expect(within(dialog).queryByRole("option", { name: "Old account" })).toBeNull();
  });

  it("E2: the copy says which existing occurrences follow and which stay", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    const text = dialog.textContent ?? "";
    expect(text).toContain("Pending occurrences already created follow a new name, category, account or amount");
    expect(text).toContain("Paid occurrences never change.");
    expect(text).toContain("occurrences already created keep their dates");
    expect(text).toContain("stop this series and create a new one");
  });

  it("E3: Save waits for a change, then PUTs exactly the changed field", async () => {
    mockApi({ "PUT /api/v1/recurring/41": () => ({ ...RENT, amount: "1250.00" }) });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    expect(save(dialog)).toBeDisabled();
    // Typing the same amount in another spelling is not a change.
    fireEvent.change(field(dialog, "Amount"), { target: { value: "1200" } });
    expect(save(dialog)).toBeDisabled();

    fireEvent.change(field(dialog, "Amount"), { target: { value: "1250" } });
    fireEvent.click(save(dialog));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(puts()).toEqual([{ url: "/api/v1/recurring/41", body: { amount: "1250" } }]);
  });

  it("E3: several edits send those keys and no others", async () => {
    mockApi({ "PUT /api/v1/recurring/41": () => RENT });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    fireEvent.change(field(dialog, "Name"), { target: { value: "  Flat rent " } });
    fireEvent.change(field(dialog, "Account"), { target: { value: "2" } });
    fireEvent.change(field(dialog, "Frequency"), { target: { value: "quarterly" } });
    fireEvent.change(field(dialog, "Next due date"), { target: { value: "2026-11-03" } });
    fireEvent.click(field(dialog, "Auto-settle"));
    fireEvent.click(save(dialog));

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0].body).toEqual({
      description: "Flat rent",
      account_id: 2,
      frequency: "quarterly",
      next_due_date: "2026-11-03",
      auto_settle: true,
    });
  });

  it("E3: an invalid amount or an empty name keeps Save disabled with a message", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    fireEvent.change(field(dialog, "Amount"), { target: { value: "1.234" } });
    expect(save(dialog)).toBeDisabled();
    expect(within(dialog).getByText("Enter an amount with up to 10 digits and 2 decimals.")).toBeInTheDocument();

    fireEvent.change(field(dialog, "Amount"), { target: { value: "1300" } });
    expect(save(dialog)).not.toBeDisabled();
    fireEvent.change(field(dialog, "Name"), { target: { value: "   " } });
    expect(save(dialog)).toBeDisabled();
    expect(within(dialog).getByText("Enter a name.")).toBeInTheDocument();
    expect(puts()).toHaveLength(0);
  });

  it("E4: an expense series is offered expense and both categories only", async () => {
    mockApi({ "PUT /api/v1/recurring/41": () => RENT });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    fireEvent.focus(field(dialog, "Category"));
    const names = categoryOptions(dialog);
    expect(names).toEqual(expect.arrayContaining(["Bills", "Groceries", "Transfers"]));
    expect(names).not.toContain("Wages");
    expect(names).not.toContain("Refunds");

    fireEvent.click(within(within(dialog).getByRole("listbox")).getByRole("option", { name: "Groceries" }));
    fireEvent.click(save(dialog));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0].body).toEqual({ category_id: 11 });
  });

  it("E4: an income series is offered income and both categories only", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Salary");
    expect(field(dialog, "Category").value).toBe("Wages");
    fireEvent.focus(field(dialog, "Category"));
    const names = categoryOptions(dialog);
    expect(names).toEqual(expect.arrayContaining(["Wages", "Refunds", "Transfers"]));
    expect(names).not.toContain("Bills");
    expect(names).not.toContain("Groceries");
  });

  it("E5: an instalment series shows N of M done, and a lower M says it stops", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Sofa");
    expect(field(dialog, "After").checked).toBe(true);
    expect(field(dialog, "Number of occurrences").value).toBe("12");
    expect(field(dialog, "Auto-settle").checked).toBe(true);
    expect(within(dialog).getByText("3 of 12 done.")).toBeInTheDocument();

    fireEvent.change(field(dialog, "Number of occurrences"), { target: { value: "2" } });
    expect(within(dialog).getByText("3 of 2 done. No more occurrences will be created.")).toBeInTheDocument();
  });

  it("E5: Never sends occurrence_count null", async () => {
    mockApi({ "PUT /api/v1/recurring/42": () => ({ ...SOFA, occurrence_count: null }) });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Sofa");
    fireEvent.click(field(dialog, "Never"));
    fireEvent.click(save(dialog));

    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0]).toEqual({ url: "/api/v1/recurring/42", body: { occurrence_count: null } });
  });

  it("E5: After N on an open-ended series sends the number; a bad number blocks Save", async () => {
    mockApi({ "PUT /api/v1/recurring/41": () => RENT });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    fireEvent.click(field(dialog, "After"));
    expect(save(dialog)).toBeDisabled();
    fireEvent.change(field(dialog, "Number of occurrences"), { target: { value: "abc" } });
    expect(within(dialog).getByText("Enter a whole number from 1 to 1200.")).toBeInTheDocument();
    expect(save(dialog)).toBeDisabled();

    fireEvent.change(field(dialog, "Number of occurrences"), { target: { value: "6" } });
    fireEvent.click(save(dialog));
    await waitFor(() => expect(puts()).toHaveLength(1));
    expect(puts()[0].body).toEqual({ occurrence_count: 6 });
  });

  it("E6: an account in another currency warns; the series' own currency does not", async () => {
    mockApi();
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    const warning = /This account is in USD\. Forecasts and totals count EUR only/;
    expect(within(dialog).queryByText(warning)).toBeNull();

    fireEvent.change(field(dialog, "Account"), { target: { value: "3" } });
    expect(within(dialog).getByText(warning)).toBeInTheDocument();

    fireEvent.change(field(dialog, "Account"), { target: { value: "2" } });
    expect(within(dialog).queryByText(warning)).toBeNull();
  });

  it("E7: the next-due guard's refusal shows in the dialog, which keeps the input", async () => {
    const detail =
      "The series has a transaction on 2026-10-01, so its next due date cannot be 2026-10-03. " +
      "Choose 2026-11-01 or later, or move or delete that transaction first.";
    mockApi({
      "PUT /api/v1/recurring/41": () => {
        throw new ApiResponseError(409, detail);
      },
    });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    fireEvent.change(field(dialog, "Next due date"), { target: { value: "2026-10-03" } });
    fireEvent.click(save(dialog));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(field(dialog, "Next due date").value).toBe("2026-10-03");
    expect(save(dialog)).not.toBeDisabled();
  });

  it("E8: a save closes the dialog, says so and reloads", async () => {
    mockApi({ "PUT /api/v1/recurring/41": () => ({ ...RENT, description: "Flat rent" }) });
    renderWithSWR(<RecurringPage />);
    const dialog = await open("Rent");
    const before = listLoads();
    fireEvent.change(field(dialog, "Name"), { target: { value: "Flat rent" } });
    fireEvent.click(save(dialog));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText('Saved "Flat rent".')).toBeInTheDocument();
    await waitFor(() => expect(listLoads()).toBeGreaterThan(before));
  });
});
