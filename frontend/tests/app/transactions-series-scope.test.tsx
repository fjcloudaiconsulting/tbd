import React from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { waitForStableTxList } from "../utils/wait-for-stable-tx-list";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => ({ get: () => null, getAll: () => [] }),
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


const TEMPLATE = {
  id: 7, account_id: ACCT_A.id, account_name: ACCT_A.name,
  category_id: CATEGORY_GROCERIES.id, category_name: CATEGORY_GROCERIES.name,
  description: "Lease", amount: 695.59, type: "expense" as const,
  frequency: "monthly", next_due_date: "2026-06-05", auto_settle: false,
  is_active: true, occurrence_count: null, occurrences_elapsed: 3,
};

function setupApiFetch(txs: Tx[], template: typeof TEMPLATE | { type: string } = TEMPLATE) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/recurring")) return [template] as never;
    if (url.startsWith("/api/v1/transactions") && method === "GET")
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
}

function putBody(id: number): Record<string, unknown> | undefined {
  const put = vi.mocked(apiFetch).mock.calls.find(
    (c) => c[0] === `/api/v1/transactions/${id}` && (c[1] as RequestInit | undefined)?.method === "PUT",
  );
  return put ? JSON.parse((put[1] as RequestInit).body as string) : undefined;
}

async function openEdit(tx: Tx, template: object = TEMPLATE) {
  setupApiFetch([tx], template as typeof TEMPLATE);
  renderWithSWR(<TransactionsPage />);
  await waitForStableTxList();
  fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
  await screen.findByTestId(`edit-recurring-chip-${tx.id}`);
}

const PENDING_LEASE = {
  id: 201, description: "Lease", amount: 695.59, status: "pending" as const,
  recurring_id: 7, date: "2026-05-05",
};

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

describe("TransactionsPage — TBD-756 series scope choice", () => {
  it("no choice while the amount and date are unchanged", async () => {
    await openEdit(makeTx(PENDING_LEASE));

    expect(screen.queryByTestId("edit-series-scope-201")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);
    await waitFor(() => expect(putBody(201)).toBeTruthy());
    expect(putBody(201)).not.toHaveProperty("series_scope");
  });

  it("a changed amount asks, with nothing pre-selected, and Save waits for an answer", async () => {
    await openEdit(makeTx(PENDING_LEASE));

    fireEvent.change(screen.getAllByLabelText("Amount")[0], { target: { value: "752.09" } });
    const choice = await screen.findByTestId("edit-series-scope-201");
    const radios = Array.from(choice.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
    expect(radios.map((r) => r.labels?.[0]?.textContent)).toEqual([
      expect.stringContaining("Only this occurrence"),
      expect.stringContaining("This and future occurrences"),
    ]);
    expect(radios.every((r) => !r.checked)).toBe(true);
    expect(screen.getByTestId("edit-series-scope-mobile-201")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);
    expect(await screen.findByText(/Choose whether the new amount or date/i)).toBeTruthy();
    expect(putBody(201)).toBeUndefined();
  });

  it("sends series_scope=future when chosen", async () => {
    await openEdit(makeTx(PENDING_LEASE));

    fireEvent.change(screen.getAllByLabelText("Date")[0], { target: { value: "2026-05-08" } });
    fireEvent.click(screen.getAllByLabelText(/This and future occurrences/)[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    await waitFor(() => expect(putBody(201)).toBeTruthy());
    expect(putBody(201)).toMatchObject({ date: "2026-05-08", series_scope: "future" });
  });

  it("sends series_scope=this when chosen", async () => {
    await openEdit(makeTx(PENDING_LEASE));

    fireEvent.change(screen.getAllByLabelText("Amount")[0], { target: { value: "752.09" } });
    fireEvent.click(screen.getAllByLabelText(/Only this occurrence/)[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    await waitFor(() => expect(putBody(201)).toBeTruthy());
    expect(putBody(201)).toMatchObject({ series_scope: "this" });
  });

  it("a settled occurrence is never offered the choice", async () => {
    await openEdit(makeTx({ ...PENDING_LEASE, status: "settled", settled_date: "2026-05-05" }));

    fireEvent.change(screen.getAllByLabelText("Amount")[0], { target: { value: "752.09" } });
    expect(screen.queryByTestId("edit-series-scope-201")).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);
    await waitFor(() => expect(putBody(201)).toBeTruthy());
    expect(putBody(201)).not.toHaveProperty("series_scope");
  });

  it("a type change applies to this occurrence only, so no choice is offered", async () => {
    await openEdit(makeTx(PENDING_LEASE));

    fireEvent.change(screen.getAllByLabelText("Amount")[0], { target: { value: "752.09" } });
    fireEvent.change(screen.getAllByLabelText("Type")[0], { target: { value: "income" } });
    expect(screen.queryByTestId("edit-series-scope-201")).toBeNull();
  });

  it("a row whose type left its series' is never offered the choice", async () => {
    await openEdit(makeTx(PENDING_LEASE), { ...TEMPLATE, type: "income" });
    // The template list loads behind the edit form; wait for it to land.
    await waitFor(() =>
      expect(vi.mocked(apiFetch).mock.calls.some((c) => c[0] === "/api/v1/recurring")).toBe(true),
    );

    fireEvent.change(screen.getAllByLabelText("Amount")[0], { target: { value: "752.09" } });
    await waitFor(() => expect(screen.queryByTestId("edit-series-scope-201")).toBeNull());
  });
});
