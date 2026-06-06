import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { waitForStableTxList } from "../utils/wait-for-stable-tx-list";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => ({ get: () => null }),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const USER = {
  id: 1, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const ACCT_A = {
  id: 100, name: "Checking A", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CATEGORY_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

type Tx = {
  id: number;
  account_id: number;
  account_name: string;
  category_id: number;
  category_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  status: "settled" | "pending";
  linked_transaction_id: number | null;
  recurring_id: number | null;
  date: string;
  settled_date: string | null;
  is_imported: boolean;
};

function makeTx(over: Partial<Tx> = {}): Tx {
  return {
    id: 1,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    description: "Coffee",
    amount: 12.5,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

function setupApiFetch(txs: Tx[]) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions") && method === "GET")
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    if (url === "/api/v1/transactions" && method === "POST") {
      return { ...makeTx(), id: 999 } as never;
    }
    return null as never;
  });
}

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
});

describe("TransactionsPage - edit row layout (Punch-list Item 7)", () => {
  it("desktop edit form renders all 7 inputs as labeled fields, not a clipped 12-col row", async () => {
    // Item 7 audit: in the legacy 12-col grid, Status was col-span-1 (~42px)
    // and Amount was col-span-1, both visibly clipping their inputs. The
    // rebalanced layout uses a labeled stacked grid (2-up on sm, 4-up on lg)
    // so each input gets >=22% of the row width. This test asserts the cell
    // containers exist and are NOT the legacy single-col-span elements.
    const tx = makeTx({ id: 70, description: "Edit me", status: "settled" });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // The new desktop edit row has the data-testid we added.
    const editRow = await screen.findByTestId("edit-row-desktop-70");
    expect(editRow).toBeTruthy();

    // Every required edit field is reachable by aria-label and is not a
    // clipped span. They are <input>/<select> inside their own cell.
    // Multiple matches expected (desktop + mobile renders both in jsdom).
    expect(screen.getAllByLabelText("Date").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Description").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Account").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Category").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Status").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Type").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Amount").length).toBeGreaterThan(0);

    // Status select shows full word labels (not glyphs); this regressed
    // pre-fix because the col-span-1 width forced abbreviations.
    const statuses = screen.getAllByLabelText("Status");
    const desktopStatus = statuses.find((el) =>
      editRow.contains(el),
    ) as HTMLSelectElement | undefined;
    expect(desktopStatus?.tagName).toBe("SELECT");
    const optionLabels = Array.from(desktopStatus!.options).map((o) => o.textContent);
    expect(optionLabels).toEqual(["Settled", "Pending"]);

    // Type select shows "Expense"/"Income" full words instead of "-"/"+".
    const types = screen.getAllByLabelText("Type");
    const desktopType = types.find((el) => editRow.contains(el)) as
      | HTMLSelectElement
      | undefined;
    expect(desktopType?.tagName).toBe("SELECT");
    const typeLabels = Array.from(desktopType!.options).map((o) => o.textContent);
    expect(typeLabels).toEqual(["Expense", "Income"]);
  });

  it("edit category picker on a transfer row lists only both-type categories", async () => {
    // A transfer's shared category must be a `both`-type category (the
    // backend rejects income/expense-only categories on transfer legs).
    // The edit picker on a transfer row must therefore offer only
    // `both`-type categories, hiding the income- and expense-only ones.
    const CAT_EXPENSE = {
      id: 11, name: "Groceries", type: "expense" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "groceries", is_system: false, transaction_count: 0,
    };
    const CAT_INCOME = {
      id: 12, name: "Salary", type: "income" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "salary", is_system: false, transaction_count: 0,
    };
    const CAT_BOTH = {
      id: 13, name: "Transfer", type: "both" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "transfer", is_system: false, transaction_count: 0,
    };

    // Transfer pair: the lower-id leg (200) is the single visible row; the
    // higher-id leg (201) is hidden but present in the list so startEdit can
    // resolve the partner synchronously and set editPartner.
    const visibleLeg = makeTx({
      id: 200, description: "Move money",
      category_id: CAT_BOTH.id, category_name: CAT_BOTH.name,
      type: "expense", linked_transaction_id: 201,
    });
    const hiddenLeg = makeTx({
      id: 201, description: "Move money",
      category_id: CAT_BOTH.id, category_name: CAT_BOTH.name,
      type: "income", linked_transaction_id: 200,
    });

    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
      if (url.startsWith("/api/v1/categories"))
        return [CAT_EXPENSE, CAT_INCOME, CAT_BOTH] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (url.startsWith("/api/v1/transactions") && method === "GET")
        return { items: [visibleLeg, hiddenLeg], total: 2, limit: 25, offset: 0 } as never;
      return null as never;
    });

    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Open the desktop transfer-row category combobox by focusing its input.
    // Both desktop + mobile CategorySelects render in jsdom; target the
    // desktop one by its id.
    const combos = await screen.findAllByRole("combobox", { name: "Category" });
    const combo = combos.find((c) => c.id === "edit-cat-200") as HTMLInputElement;
    expect(combo).toBeTruthy();
    fireEvent.focus(combo);

    // Scope option assertions to the desktop picker's listbox so the mobile
    // picker (still closed) can't affect the result.
    const listbox = await screen.findByRole("listbox");
    const optionNames = within(listbox)
      .getAllByRole("option")
      .map((o) => o.textContent ?? "");
    expect(optionNames.some((n) => /Transfer/.test(n))).toBe(true);
    expect(optionNames.some((n) => /Groceries/.test(n))).toBe(false);
    expect(optionNames.some((n) => /Salary/.test(n))).toBe(false);
  });

  it("transfer edit picker stays both-only even when the partner leg is off-page (editPartner unresolved)", async () => {
    // With server-side pagination the partner leg can be absent from the
    // current page, so startEdit resolves editPartner asynchronously (and
    // here never — the partner fetch returns null). The both-only constraint
    // must NOT depend on editPartner: it is keyed off the synchronously-known
    // tx.linked_transaction_id (isTransfer). Regression guard for the picker
    // briefly showing income/expense-only categories during that window.
    const CAT_EXPENSE = {
      id: 11, name: "Groceries", type: "expense" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "groceries", is_system: false, transaction_count: 0,
    };
    const CAT_INCOME = {
      id: 12, name: "Salary", type: "income" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "salary", is_system: false, transaction_count: 0,
    };
    const CAT_BOTH = {
      id: 13, name: "Transfer", type: "both" as const,
      parent_id: null, parent_name: null, description: null,
      slug: "transfer", is_system: false, transaction_count: 0,
    };

    // Only the visible leg (200) is on this page; its partner (201) is NOT
    // in the list, so startEdit must fetch it. We resolve that fetch to null
    // so editPartner stays null for the whole assertion — isolating the
    // isTransfer-driven constraint from editPartner.
    const visibleLeg = makeTx({
      id: 200, description: "Move money",
      category_id: CAT_BOTH.id, category_name: CAT_BOTH.name,
      type: "expense", linked_transaction_id: 201,
    });

    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
      if (url.startsWith("/api/v1/categories"))
        return [CAT_EXPENSE, CAT_INCOME, CAT_BOTH] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      // Single-transaction GET for the off-page partner: resolve null so
      // editPartner is never set.
      if (url === "/api/v1/transactions/201" && method === "GET")
        return null as never;
      if (url.startsWith("/api/v1/transactions") && method === "GET")
        return { items: [visibleLeg], total: 1, limit: 25, offset: 0 } as never;
      return null as never;
    });

    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    const combos = await screen.findAllByRole("combobox", { name: "Category" });
    const combo = combos.find((c) => c.id === "edit-cat-200") as HTMLInputElement;
    expect(combo).toBeTruthy();
    fireEvent.focus(combo);

    const listbox = await screen.findByRole("listbox");
    const optionNames = within(listbox)
      .getAllByRole("option")
      .map((o) => o.textContent ?? "");
    expect(optionNames.some((n) => /Transfer/.test(n))).toBe(true);
    expect(optionNames.some((n) => /Groceries/.test(n))).toBe(false);
    expect(optionNames.some((n) => /Salary/.test(n))).toBe(false);
  });

  it("desktop edit form Save/Cancel meet the 44px touch-target floor", async () => {
    // Tablet portrait (md+) still receives the desktop layout, so a 36px
    // tap target would fall below the project a11y baseline shipped in
    // PRs #173/#174. Mirror the mobile-form action floor here.
    const tx = makeTx({ id: 71, description: "Touch me" });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    const editRow = await screen.findByTestId("edit-row-desktop-71");
    const saves = screen.getAllByRole("button", { name: /^Save$/ });
    const desktopSave = saves.find((el) => editRow.contains(el));
    expect(desktopSave).toBeTruthy();
    expect(desktopSave!.className).toMatch(/min-h-\[44px\]/);

    const cancels = screen.getAllByRole("button", { name: /^Cancel$/ });
    const desktopCancel = cancels.find((el) => editRow.contains(el));
    expect(desktopCancel).toBeTruthy();
    expect(desktopCancel!.className).toMatch(/min-h-\[44px\]/);
  });
});

describe("TransactionsPage - settled_date (Punch-list Item 13)", () => {
  it("create form: settled date field shown ONLY when status=pending", async () => {
    setupApiFetch([]);
    render(<TransactionsPage />);

    // Default status=settled -> field hidden.
    fireEvent.click(await screen.findByRole("button", { name: /\+ New Transaction/i }));
    expect(screen.queryByLabelText(/Expected settlement/i)).toBeNull();

    // Switch to pending -> field appears.
    const status = screen.getByLabelText("Status") as HTMLSelectElement;
    fireEvent.change(status, { target: { value: "pending" } });
    expect(screen.getByLabelText(/Expected settlement/i)).toBeTruthy();

    // Back to settled -> hidden again.
    fireEvent.change(status, { target: { value: "settled" } });
    expect(screen.queryByLabelText(/Expected settlement/i)).toBeNull();
  });

  it("create form: posts settled_date when pending + value entered", async () => {
    setupApiFetch([]);
    render(<TransactionsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ New Transaction/i }));

    await waitFor(() => {
      const acct = screen.getByLabelText(/^Account$/i) as HTMLSelectElement;
      expect(acct.value).not.toBe("");
    });

    // Fill required fields (account is auto-selected from the default).
    fireEvent.change(screen.getByLabelText(/Description/i), {
      target: { value: "CC purchase" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "42" },
    });
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "pending" },
    });
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2026-05-01" },
    });
    const expectedField = screen.getByLabelText(/Expected settlement/i);
    fireEvent.change(expectedField, { target: { value: "2026-06-15" } });

    fireEvent.click(screen.getByRole("button", { name: /Add Transaction/i }));

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const post = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions" &&
          (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
    });
    const post = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    const body = JSON.parse((post[1] as RequestInit).body as string);
    expect(body.settled_date).toBe("2026-06-15");
    expect(body.status).toBe("pending");
  });

  it("create form: rejects settled_date earlier than transaction date inline", async () => {
    setupApiFetch([]);
    render(<TransactionsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /\+ New Transaction/i }));

    // Wait for the default-account auto-fill effect to run so the required
    // Account select is populated. Without this, jsdom's native HTML5
    // validation blocks form submit before our handler runs.
    await waitFor(() => {
      const acct = screen.getByLabelText(/^Account$/i) as HTMLSelectElement;
      expect(acct.value).not.toBe("");
    });

    fireEvent.change(screen.getByLabelText(/Description/i), {
      target: { value: "Bad date" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2026-05-10" },
    });
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "pending" },
    });
    fireEvent.change(screen.getByLabelText(/Expected settlement/i), {
      target: { value: "2026-05-01" }, // before the date
    });

    // Submit the form directly. jsdom's HTML5 validation on the form's
    // required fields runs on click-submit and can pre-empt our handler;
    // dispatching `submit` exercises the same code path React listens to.
    const form = screen.getByRole("button", { name: /Add Transaction/i }).closest("form")!;
    fireEvent.submit(form);

    // Inline error renders. The submit must NOT have called POST.
    expect(
      await screen.findByText(/must be on or after the transaction date/i),
    ).toBeTruthy();
    const apiFetchMock = vi.mocked(apiFetch);
    const post = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    );
    expect(post).toBeUndefined();
  });

  it("edit form: settled date pre-fills from server when pending row already has one", async () => {
    const tx = makeTx({
      id: 80,
      description: "Pending CC",
      status: "pending",
      date: "2026-05-01",
      settled_date: "2026-06-10",
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    const inputs = await screen.findAllByLabelText(/Expected settlement/i);
    expect((inputs[0] as HTMLInputElement).value).toBe("2026-06-10");
  });

  it("edit form: PUT body includes settled_date when status=pending and value set", async () => {
    const tx = makeTx({
      id: 81,
      description: "Pending CC",
      status: "pending",
      date: "2026-05-01",
      settled_date: null,
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    const expectedFields = await screen.findAllByLabelText(/Expected settlement/i);
    fireEvent.change(expectedFields[0], { target: { value: "2026-06-15" } });

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const put = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/81" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const put = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/81" &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    )!;
    const body = JSON.parse((put[1] as RequestInit).body as string);
    expect(body.settled_date).toBe("2026-06-15");
    expect(body.status).toBe("pending");
  });

  it("edit form: settled_date NOT sent when row stays settled", async () => {
    const tx = makeTx({
      id: 82,
      description: "Plain settled",
      status: "settled",
      date: "2026-05-01",
      settled_date: "2026-05-01",
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Field is hidden when settled.
    expect(screen.queryByLabelText(/Expected settlement/i)).toBeNull();

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const put = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/82" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const put = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/82" &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    )!;
    const body = JSON.parse((put[1] as RequestInit).body as string);
    expect(body).not.toHaveProperty("settled_date");
  });

  it("view row: shows 'expected settled YYYY-MM-DD' subtext on pending rows", async () => {
    const tx = makeTx({
      id: 90,
      description: "Pending CC",
      status: "pending",
      date: "2026-05-01",
      settled_date: "2026-06-15",
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await screen.findAllByText("Pending CC");
    // Subtext renders in both desktop + mobile views.
    const subtexts = await screen.findAllByText(/expected settled 2026-06-15/);
    expect(subtexts.length).toBeGreaterThan(0);
  });

  it("view row: subtext hidden when settled_date matches transaction date (would be redundant)", async () => {
    const tx = makeTx({
      id: 91,
      description: "Pending same date",
      status: "pending",
      date: "2026-05-01",
      settled_date: "2026-05-01",
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await screen.findAllByText("Pending same date");
    expect(screen.queryByText(/expected settled/i)).toBeNull();
  });

  it("edit form: clearing the expected-settlement input sends settled_date: null (not omitted)", async () => {
    // Regression: the frontend used to send the field when set, but on clear
    // the empty-string -> ?? -> undefined path made the JSON body OMIT the
    // key. Combined with a backend that only updated on non-null, clearing
    // was a silent no-op. The frontend now sends explicit null on clear so
    // the backend can wipe the persisted value.
    const tx = makeTx({
      id: 95,
      description: "Pending CC clearable",
      status: "pending",
      date: "2026-05-01",
      settled_date: "2026-06-15",
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Pre-fill confirms the wired-from-server path.
    const expectedFields = await screen.findAllByLabelText(/Expected settlement/i);
    expect((expectedFields[0] as HTMLInputElement).value).toBe("2026-06-15");

    // Clear the field.
    fireEvent.change(expectedFields[0], { target: { value: "" } });

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const put = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/95" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      );
      expect(put).toBeTruthy();
    });
    const put = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/95" &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    )!;
    const body = JSON.parse((put[1] as RequestInit).body as string);
    // The key MUST be present and explicitly null. Asserting "in" plus a
    // strict-null check guards against a future regression where the
    // frontend silently drops the key when the input is empty.
    expect("settled_date" in body).toBe(true);
    expect(body.settled_date).toBeNull();
  });

  it("edit form: rejects settled_date earlier than transaction date inline", async () => {
    const tx = makeTx({
      id: 92,
      description: "Pending edit bad",
      status: "pending",
      date: "2026-05-10",
      settled_date: null,
    });
    setupApiFetch([tx]);
    render(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    const expectedFields = await screen.findAllByLabelText(/Expected settlement/i);
    fireEvent.change(expectedFields[0], { target: { value: "2026-05-01" } });

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    expect(
      await screen.findByText(/must be on or after the transaction date/i),
    ).toBeTruthy();
    // No PUT should have fired.
    const apiFetchMock = vi.mocked(apiFetch);
    const put = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/92" &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    );
    expect(put).toBeUndefined();
  });
});
