import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
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

const CAT_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};
const CAT_DINING = {
  id: 12, name: "Dining", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "dining", is_system: false, transaction_count: 0,
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
    category_id: CAT_GROCERIES.id,
    category_name: CAT_GROCERIES.name,
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

type BulkResponse = {
  requested_count: number;
  updated_count: number;
  skipped: { id: number; reason: string }[];
  notes?: { id: number; note: string }[];
};

function setupApiFetch(txs: Tx[], bulkResponse: BulkResponse) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
    if (url.startsWith("/api/v1/categories")) return [CAT_GROCERIES, CAT_DINING] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url === "/api/v1/transactions/bulk-update" && method === "POST")
      return bulkResponse as never;
    if (url.startsWith("/api/v1/transactions") && method === "GET")
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
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

async function selectAllAndOpenModal() {
  await waitForStableTxList();
  // Select every row via the desktop select-all header checkbox.
  fireEvent.click(screen.getByLabelText("Select all on page"));
  // Toolbar appears with the count + Batch edit action.
  const batchBtns = await screen.findAllByRole("button", { name: /^Batch edit$/i });
  fireEvent.click(batchBtns[0]);
  return screen.getByRole("dialog");
}

describe("TransactionsPage - batch edit wiring", () => {
  it("posts bulk-update with selected ids + category and reloads the list", async () => {
    const txs = [
      makeTx({ id: 70, description: "One" }),
      makeTx({ id: 71, description: "Two" }),
    ];
    setupApiFetch(txs, { requested_count: 2, updated_count: 2, skipped: [] });
    renderWithSWR(<TransactionsPage />);

    const dialog = await selectAllAndOpenModal();

    // Set a category inside the modal.
    const combo = within(dialog).getByRole("combobox", { name: /category/i });
    fireEvent.focus(combo);
    const listbox = await within(dialog).findByRole("listbox");
    fireEvent.click(within(listbox).getByText("Dining"));

    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const post = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/bulk-update" &&
          (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
    });

    const post = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/bulk-update" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    const body = JSON.parse((post[1] as RequestInit).body as string);
    expect(body.ids.slice().sort((a: number, b: number) => a - b)).toEqual([70, 71]);
    expect(body.category_id).toBe(CAT_DINING.id);

    // List reloads: at least one more GET fires after the POST.
    const postIdx = apiFetchMock.mock.calls.indexOf(post);
    await waitFor(() => {
      const reload = apiFetchMock.mock.calls
        .slice(postIdx + 1)
        .find(
          (c) =>
            typeof c[0] === "string" &&
            (c[0] as string).startsWith("/api/v1/transactions?") &&
            ((c[1] as RequestInit | undefined)?.method ?? "GET") === "GET",
        );
      expect(reload).toBeTruthy();
    });
  });

  it("surfaces a skipped summary in the error banner when the response has skips", async () => {
    const txs = [
      makeTx({ id: 80, description: "One" }),
      makeTx({ id: 81, description: "Two" }),
    ];
    setupApiFetch(txs, {
      requested_count: 2,
      updated_count: 1,
      skipped: [{ id: 81, reason: "Manual balance adjustments cannot be edited" }],
    });
    renderWithSWR(<TransactionsPage />);

    const dialog = await selectAllAndOpenModal();

    const combo = within(dialog).getByRole("combobox", { name: /category/i });
    fireEvent.focus(combo);
    const listbox = await within(dialog).findByRole("listbox");
    fireEvent.click(within(listbox).getByText("Dining"));
    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    expect(
      await screen.findByText(/Updated 1 of 2\..*1 skipped.*Manual balance adjustments/i),
    ).toBeTruthy();
  });
  // TBD-763
  it("posts the two dates with the selected ids", async () => {
    const txs = [
      makeTx({ id: 90, description: "One" }),
      makeTx({ id: 91, description: "Two" }),
    ];
    setupApiFetch(txs, { requested_count: 2, updated_count: 2, skipped: [], notes: [] });
    renderWithSWR(<TransactionsPage />);

    const dialog = await selectAllAndOpenModal();
    fireEvent.change(within(dialog).getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^set settlement date$/i), {
      target: { value: "2026-05-12" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const post = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/bulk-update" &&
          (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
    });
    const post = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/bulk-update" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    const body = JSON.parse((post[1] as RequestInit).body as string);
    expect(Object.keys(body).sort()).toEqual(["date", "ids", "settled_date"]);
    expect(body.ids.slice().sort((a: number, b: number) => a - b)).toEqual([90, 91]);
    expect(body.date).toBe("2026-05-10");
    expect(body.settled_date).toBe("2026-05-12");
    // Nothing to report: no notes banner.
    await waitFor(() =>
      expect(screen.queryByTestId("transactions-batch-edit-notes")).toBeNull(),
    );
  });

  it("announces fields a row did not take in the status region, not as an error", async () => {
    const txs = [
      makeTx({ id: 92, description: "One" }),
      makeTx({ id: 93, description: "Two", recurring_id: 5 }),
      makeTx({ id: 94, description: "Three", recurring_id: 5 }),
    ];
    setupApiFetch(txs, {
      requested_count: 3,
      updated_count: 3,
      skipped: [],
      notes: [
        { id: 93, note: "date not applied to recurring occurrences" },
        { id: 94, note: "date not applied to recurring occurrences" },
      ],
    });
    renderWithSWR(<TransactionsPage />);

    const dialog = await selectAllAndOpenModal();
    fireEvent.change(within(dialog).getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    const banner = await screen.findByTestId("transactions-batch-edit-notes");
    // The same note on two rows is said once, with the row count.
    expect(banner.textContent).toBe(
      "Updated 3 of 3. 2 kept a field unchanged: date not applied to recurring occurrences",
    );
    expect(screen.getByTestId("transactions-live-region").contains(banner)).toBe(true);
    expect(screen.getByTestId("transactions-live-region").getAttribute("role")).toBe("status");
    expect(banner.className).not.toMatch(/danger/);
  });
  it("says an identical skip reason once, like the notes", async () => {
    const txs = [
      makeTx({ id: 95, description: "One" }),
      makeTx({ id: 96, description: "Two" }),
    ];
    const reason = "date not applied: settled on 2026-05-01; set the settlement date too";
    setupApiFetch(txs, {
      requested_count: 2,
      updated_count: 0,
      skipped: [{ id: 95, reason }, { id: 96, reason }],
      notes: [],
    });
    renderWithSWR(<TransactionsPage />);

    const dialog = await selectAllAndOpenModal();
    fireEvent.change(within(dialog).getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    expect(await screen.findByText(`Updated 0 of 2. 2 skipped: ${reason}`)).toBeTruthy();
  });
});
