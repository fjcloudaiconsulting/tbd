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

// total of 30 rows on the server (> default page size 25 => Pagination renders).
const SERVER_TOTAL = 30;

// Module-level record of every LIST URL the page requested.
let urls: string[] = [];

// Only the top-level list endpoint, excluding sub-paths like
// /transactions/30, /transactions/30/tags, /transactions/bulk-delete.
function listUrls(): string[] {
  return urls.filter((u) => /^\/api\/v1\/transactions\?/.test(u));
}

function paramOf(url: string, key: string): string | null {
  const q = url.split("?")[1] ?? "";
  return new URLSearchParams(q).get(key);
}

// NOTE: TransactionsPage reads accounts/categories/billing-periods via SWR,
// whose default cache is warm across this file's `it` blocks. These tests are
// safe on plain render() because setupApiFetch returns CONSTANT refs. If you add
// a case that needs DIFFERENT ref data, switch it to renderWithSWR (fresh cache)
// or it will silently receive an earlier test's cached refs.
function setupApiFetch() {
  urls = [];
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;

    // List endpoint (has a query string). Record it and return a page-sized
    // slice driven by the requested limit/offset.
    if (/^\/api\/v1\/transactions\?/.test(url)) {
      urls.push(url);
      const limit = Number(paramOf(url, "limit") ?? 25);
      const offset = Number(paramOf(url, "offset") ?? 0);
      const items = [];
      for (let i = 0; i < limit && offset + i < SERVER_TOTAL; i++) {
        const id = offset + i + 1;
        items.push(
          makeTx({
            id,
            description: `Row ${id}`,
            amount: 10 + id,
          }),
        );
      }
      return { items, total: SERVER_TOTAL, limit, offset } as never;
    }
    return null as never;
  });
}

async function waitForStableTxList() {
  // Page kicks off loadRefs() + loadTransactions(0); the list effect re-fires
  // (setFetching(true) -> Spinner) once `periods` settles, then resolves to
  // the table. findAllByText drives the act() flush through the full
  // spinner -> table settle (this is the same settle pattern the sibling
  // transactions-page test relies on). Once a row is visible the page is in
  // its non-fetching branch, so the shared Pagination has mounted too.
  await screen.findAllByText("Row 1", undefined, { timeout: 8000 });
  // The shared Pagination (which owns the "Per page" / "Next page"
  // controls) mounts one React tick after the rows, once `total` state
  // propagates. Under parallel-worker CI load that tick can lag, so settle
  // on the control asynchronously with a generous timeout. A synchronous
  // getByLabelText here (or at the interaction sites below) races and
  // intermittently fails the whole file in full-suite order.
  await screen.findByLabelText(/per page/i, undefined, { timeout: 8000 });
}

describe("TransactionsPage — server-side pagination/sort/selection (Task 4)", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    window.localStorage.clear();
    useAuthMock.mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
    setupApiFetch();
  });

  it("renders shared Pagination with real total", async () => {
    render(<TransactionsPage />);
    await waitForStableTxList();

    // Per-page selector present. Use findBy: the pagination bar renders a
    // tick after the rows (once `total` state propagates), so a sync query
    // races under slower CI timing.
    expect(await screen.findByLabelText(/per page/i)).toBeInTheDocument();

    // Status line: 30 total / 25 per page => 2 pages.
    await waitFor(() => {
      expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
    });
    expect(screen.getByText(/30 total/)).toBeInTheDocument();
  });

  it("Next page fetches offset=25 (limit=25)", async () => {
    render(<TransactionsPage />);
    await waitForStableTxList();

    fireEvent.click(
      await screen.findByLabelText("Next page", undefined, { timeout: 8000 }),
    );

    await waitFor(() => {
      expect(
        listUrls().some(
          (u) => u.includes("offset=25") && u.includes("limit=25"),
        ),
      ).toBe(true);
    });
  });

  it("sort header resets to offset=0 and sends sort params", async () => {
    render(<TransactionsPage />);
    await waitForStableTxList();

    // First move off page 0 so a later offset=0 fetch is unambiguously the
    // sort's doing, not the initial load.
    fireEvent.click(
      await screen.findByLabelText("Next page", undefined, { timeout: 8000 }),
    );
    await waitFor(() => {
      expect(listUrls().some((u) => u.includes("offset=25"))).toBe(true);
    });

    // Click the "Description" column header (toggleSort("description")).
    fireEvent.click(screen.getByRole("button", { name: /^Description/ }));

    await waitFor(() => {
      expect(
        listUrls().some(
          (u) =>
            u.includes("sort_by=description") &&
            u.includes("sort_dir=") &&
            u.includes("offset=0"),
        ),
      ).toBe(true);
    });
  });

  it("per-page selector changes limit and resets offset", async () => {
    render(<TransactionsPage />);
    await waitForStableTxList();

    fireEvent.change(
      await screen.findByLabelText(/per page/i, undefined, { timeout: 8000 }),
      { target: { value: "50" } },
    );

    await waitFor(() => {
      expect(
        listUrls().some(
          (u) => u.includes("limit=50") && u.includes("offset=0"),
        ),
      ).toBe(true);
    });
  });

  it("per-page size persists across remount", async () => {
    const { unmount } = render(<TransactionsPage />);
    await waitForStableTxList();

    // Change the page size; this writes to localStorage.
    fireEvent.change(
      await screen.findByLabelText(/per page/i, undefined, { timeout: 8000 }),
      { target: { value: "50" } },
    );
    await waitFor(() => {
      expect(
        listUrls().some((u) => u.includes("limit=50")),
      ).toBe(true);
    });

    // Unmount, then drop the recorded URLs so the next assertion only
    // sees the fresh mount's fetches. localStorage is intentionally NOT
    // cleared (beforeEach clears once; the two renders share it).
    unmount();
    urls = [];

    // Fresh mount rehydrates the persisted page size. We can't reuse
    // waitForStableTxList here: with pageSize=50 and 30 total rows the
    // page renders a single page, and the page only mounts <Pagination>
    // (which owns the "Per page" control) when `total > pageSize ||
    // page > 0`. So settle on a visible row instead, then assert the
    // rehydrated fetch carried the persisted limit.
    render(<TransactionsPage />);
    await screen.findAllByText("Row 1", undefined, { timeout: 4000 });

    await waitFor(() => {
      expect(
        listUrls().some((u) => u.includes("limit=50")),
      ).toBe(true);
    });
  });

  it("selection clears on navigation", async () => {
    render(<TransactionsPage />);
    await waitForStableTxList();

    // Wait for the per-row checkbox to render, then select row 1.
    await waitFor(() => {
      expect(
        screen.queryAllByLabelText("Select transaction 1").length,
      ).toBeGreaterThan(0);
    });
    fireEvent.click(screen.getAllByLabelText("Select transaction 1")[0]);

    // Bulk bar appears with the "N selected" count text.
    await waitFor(() => {
      expect(screen.getAllByText(/^\d+ selected$/).length).toBeGreaterThan(0);
    });

    // Navigate to the next page; selection must clear (count text gone).
    fireEvent.click(
      await screen.findByLabelText("Next page", undefined, { timeout: 8000 }),
    );

    await waitFor(() => {
      expect(screen.queryAllByText(/^\d+ selected$/).length).toBe(0);
    });
  });
});
