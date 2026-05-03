import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

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

const ACCT_B = {
  id: 200, name: "Checking B", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: false,
};

const CATEGORY_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function makeTx(over: Partial<{
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
}> = {}) {
  return {
    id: 1,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    description: "Tx",
    amount: 100,
    type: "expense" as const,
    status: "settled" as const,
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

function setupApiFetch(txs: ReturnType<typeof makeTx>[]) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  // The page kicks off loadRefs() (3 calls in parallel) and loadTransactions(0).
  // We can't rely on order due to Promise.all, but the URL identifies the route.
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions")) return txs as never;
    return null as never;
  });
}

describe("TransactionsPage — transfer wiring (Task D7)", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    useAuthMock.mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("Bulk-toolbar Link as transfer button: enabled when 2 valid rows selected, disabled with reason otherwise", async () => {
    const expenseTx = makeTx({
      id: 1, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 100, description: "Out",
    });
    const incomeTx = makeTx({
      id: 2, account_id: ACCT_B.id, account_name: ACCT_B.name,
      type: "income", amount: 100, description: "In",
    });
    const otherIncomeTx = makeTx({
      id: 3, account_id: ACCT_B.id, account_name: ACCT_B.name,
      type: "income", amount: 250, description: "Different amount",
    });
    setupApiFetch([expenseTx, incomeTx, otherIncomeTx]);

    render(<TransactionsPage />);

    // Wait for the page to fetch + render rows. Both desktop+mobile layouts
    // render in jsdom so use findAllByText to tolerate duplicates.
    await screen.findAllByText("Out");
    await screen.findAllByText("In");
    await screen.findAllByText("Different amount");

    // Select expense (id=1) and income (id=2). The selection toolbar should
    // render and the Link button should be enabled. Both desktop+mobile
    // checkboxes share the aria-label, so click the first.
    fireEvent.click(screen.getAllByLabelText("Select transaction 1")[0]);
    fireEvent.click(screen.getAllByLabelText("Select transaction 2")[0]);

    const linkBtn = await screen.findByRole("button", { name: /link as transfer/i });
    expect(linkBtn).toBeEnabled();

    // Switch the selection: deselect id=2 (matching income) and select id=3
    // (mismatched amount). Button should now be visible+disabled with a tooltip
    // mentioning amount.
    fireEvent.click(screen.getAllByLabelText("Select transaction 2")[0]);
    fireEvent.click(screen.getAllByLabelText("Select transaction 3")[0]);

    await waitFor(() => {
      const refreshed = screen.getByRole("button", { name: /link as transfer/i });
      expect(refreshed).toBeDisabled();
      expect(refreshed.getAttribute("title")?.toLowerCase()).toContain("amount");
    });
  });

  it("Edit visible on linked rows with mirror-amount notice and read-only type", async () => {
    const expenseLeg = makeTx({
      id: 10, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 50, description: "Transfer out",
      linked_transaction_id: 11,
    });
    const incomeLeg = makeTx({
      id: 11, account_id: ACCT_B.id, account_name: ACCT_B.name,
      type: "income", amount: 50, description: "Transfer in",
      linked_transaction_id: 10,
    });
    setupApiFetch([expenseLeg, incomeLeg]);

    render(<TransactionsPage />);

    // Wait for rows to render. The pair-dedupe rule keeps the lower-id row
    // visible; the partner (id 11) is hidden because 11 > 10.
    await screen.findAllByText("Transfer out");

    // Wait for the loading spinner to be gone (the page loads transactions
    // twice because the loadTransactions effect depends on `periods`).
    await waitFor(() => {
      expect(screen.queryAllByRole("button", { name: /^Edit:/ }).length).toBeGreaterThan(0);
    });

    // Click Edit on the visible linked row (desktop + mobile each render one).
    const editButtons = screen.getAllByRole("button", { name: /^Edit:/ });
    fireEvent.click(editButtons[0]);

    // Mirror-amount notice should now render (one for desktop, one for mobile).
    const notices = await screen.findAllByText(/Changes to amount apply to both rows/i);
    expect(notices.length).toBeGreaterThan(0);

    // Type field is read-only (rendered as a span with title hint, not a select).
    const typeNodes = screen.getAllByLabelText("Type");
    // None should be a SELECT element while editing a linked leg.
    expect(typeNodes.some((el) => el.tagName === "SELECT")).toBe(false);
    expect(typeNodes[0].getAttribute("title")).toMatch(/transfer leg/i);
  });

  it("Saving an edit on a linked row omits 'type' from the PUT body", async () => {
    const expenseLeg = makeTx({
      id: 30, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 50, description: "Linked out",
      linked_transaction_id: 31,
    });
    const incomeLeg = makeTx({
      id: 31, account_id: ACCT_B.id, account_name: ACCT_B.name,
      type: "income", amount: 50, description: "Linked in",
      linked_transaction_id: 30,
    });
    setupApiFetch([expenseLeg, incomeLeg]);

    render(<TransactionsPage />);

    await screen.findAllByText("Linked out");
    await waitFor(() => {
      expect(screen.queryAllByRole("button", { name: /^Edit:/ }).length).toBeGreaterThan(0);
    });

    // Open edit on the visible (lower-id) linked row.
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Change description to something different.
    const descInputs = screen.getAllByLabelText("Description");
    fireEvent.change(descInputs[0], { target: { value: "Linked out edited" } });

    // Click Save.
    const saveBtns = screen.getAllByRole("button", { name: /^Save$/i });
    fireEvent.click(saveBtns[0]);

    // Find the PUT call to /api/v1/transactions/30.
    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const putCall = apiFetchMock.mock.calls.find(
        (call) =>
          call[0] === "/api/v1/transactions/30" &&
          (call[1] as RequestInit | undefined)?.method === "PUT"
      );
      expect(putCall).toBeTruthy();
    });

    const putCall = apiFetchMock.mock.calls.find(
      (call) =>
        call[0] === "/api/v1/transactions/30" &&
        (call[1] as RequestInit | undefined)?.method === "PUT"
    )!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body).not.toHaveProperty("type");
    expect(body.description).toBe("Linked out edited");
  });

  it("Per-row Mark as transfer button shown on un-linked rows only", async () => {
    const linked = makeTx({
      id: 20, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 75, description: "Linked tx",
      linked_transaction_id: 21,
    });
    const linkedPartner = makeTx({
      id: 21, account_id: ACCT_B.id, account_name: ACCT_B.name,
      type: "income", amount: 75, description: "Linked partner",
      linked_transaction_id: 20,
    });
    const unlinked = makeTx({
      id: 22, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 30, description: "Unlinked tx",
      linked_transaction_id: null,
    });
    setupApiFetch([linked, linkedPartner, unlinked]);

    render(<TransactionsPage />);

    await screen.findAllByText("Linked tx");
    await screen.findAllByText("Unlinked tx");

    // Wait for the action buttons to be present (rows have to fully render).
    await waitFor(() => {
      expect(
        screen.queryAllByRole("button", { name: /Mark as transfer: Unlinked tx/i }).length,
      ).toBeGreaterThan(0);
    });

    // Mark-as-transfer button must exist for the unlinked row...
    const markBtns = screen.getAllByRole("button", { name: /Mark as transfer: Unlinked tx/i });
    expect(markBtns.length).toBeGreaterThan(0);

    // ...and must NOT exist for the linked row.
    expect(screen.queryByRole("button", { name: /Mark as transfer: Linked tx/i })).toBeNull();

    // The linked row exposes an Unlink button instead.
    const unlinkBtns = screen.getAllByRole("button", { name: /Unlink transfer: Linked tx/i });
    expect(unlinkBtns.length).toBeGreaterThan(0);
  });
});
