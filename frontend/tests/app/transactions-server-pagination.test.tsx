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
  linked_account_name: string | null;
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
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

type Tx = ReturnType<typeof makeTx>;

// A reciprocally-linked transfer pair, expense leg on `loAccount` taking the
// LOWER id. Mirrors create_transfer, which adds the expense leg first so the
// income leg reliably takes the higher id.
function makePair(
  loId: number,
  hiId: number,
  opts: { loAccount?: typeof ACCT_A; hiAccount?: typeof ACCT_B; amount?: number } = {},
): [Tx, Tx] {
  const lo = opts.loAccount ?? ACCT_A;
  const hi = opts.hiAccount ?? ACCT_B;
  const amount = opts.amount ?? 50;
  return [
    makeTx({
      id: loId, account_id: lo.id, account_name: lo.name,
      linked_transaction_id: hiId, linked_account_name: hi.name,
      type: "expense", amount, description: `Row ${loId}`,
    }),
    makeTx({
      id: hiId, account_id: hi.id, account_name: hi.name,
      linked_transaction_id: loId, linked_account_name: lo.name,
      type: "income", amount, description: `Row ${hiId}`,
    }),
  ];
}

// The rows the "server" holds, in server-sort order. Replaced per test via
// setupApiFetch(corpus). Defaults to 30 pair-free rows, which keeps the
// original "Page 1 of 2" / "30 total" assertions below as CONTROLS: they must
// stay green while the collapse fences move.
const DEFAULT_CORPUS: Tx[] = Array.from({ length: 30 }, (_, i) =>
  makeTx({ id: i + 1, description: `Row ${i + 1}`, amount: 10 + i + 1 }),
);

// Server-side transfer collapse, faithful to
// transaction_service._transfer_collapse_clause. Applied to the whole corpus
// BEFORE limit/offset — which is the entire point of TBD-268.
function collapse(corpus: Tx[]): Tx[] {
  const byId = new Map(corpus.map((t) => [t.id, t]));
  return corpus.filter((t) => {
    if (t.linked_transaction_id == null) return true;              // 1
    if (t.linked_transaction_id === t.id) return true;             // 2
    const partner = byId.get(t.linked_transaction_id);
    if (!partner) return true;                                     // 3 + 5
    if (partner.linked_transaction_id !== t.id) return true;       // 3
    return t.id < t.linked_transaction_id;                         // 4
  });
}

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
function setupApiFetch(corpus: Tx[] = DEFAULT_CORPUS) {
  urls = [];
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;

    // List endpoint (has a query string). A MINI-SERVER, not a slicer: when
    // collapse_transfers=true it folds the pairs over the WHOLE corpus first
    // and only then applies limit/offset, and reports the post-collapse total.
    // Without that ordering F1 cannot go red on a missing param.
    if (/^\/api\/v1\/transactions\?/.test(url)) {
      urls.push(url);
      const limit = Number(paramOf(url, "limit") ?? 25);
      const offset = Number(paramOf(url, "offset") ?? 0);
      const rows = paramOf(url, "collapse_transfers") === "true"
        ? collapse(corpus)
        : corpus;
      const items = rows.slice(offset, offset + limit);
      return { items, total: rows.length, limit, offset } as never;
    }
    return null as never;
  });
}

async function waitForStableTxList() {
  // Page mounts the SWR refs and, once periods have settled, issues a single
  // loadTransactions(0) that resolves the Spinner to the table. findAllByText
  // drives the act() flush through the spinner -> table settle (the same settle
  // pattern the sibling transactions-page test relies on). Once a row is
  // visible the page is in its non-fetching branch, so the shared Pagination
  // has mounted too.
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

// ── TBD-268 fences: server-side transfer collapse ─────────────────────────
//
// Row counting uses the per-row test ids, NOT the checkbox / delete aria
// labels. The desktop grid is `hidden md:block` and the mobile list is
// `md:hidden` — CSS-only, so jsdom renders BOTH and any label-based count
// returns 2x the row count. (`Delete: ${description}` additionally collides
// across both legs of a create_transfer pair, which share one auto-generated
// description.)
function desktopRowCount(): number {
  return screen.getAllByTestId(/^tx-row-desktop-\d+$/).length;
}

function desktopRowIds(): number[] {
  return screen
    .getAllByTestId(/^tx-row-desktop-\d+$/)
    .map((el) => Number(el.getAttribute("data-testid")!.replace("tx-row-desktop-", "")));
}

describe("TransactionsPage — transfer collapse (TBD-268)", () => {
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
  });

  // 34 raw rows -> 30 collapsed: 25 singles, 4 mutual pairs (8 raw -> 4), and
  // one leg whose partner is NOT in the corpus and holds a LOWER id.
  //
  // FIXTURE TRAP this exists to avoid: that last row is the whole point. It is
  // a row with `linked_transaction_id != null && id > linked_transaction_id`
  // that the server legitimately returns (the partner was excluded by a filter
  // or a page boundary). Without it, a LEFTOVER CLIENT HIDE is inert against
  // this corpus and F1 passes with the bug still present.
  function collapseCorpus(): Tx[] {
    const rows: Tx[] = [
      // FIRST in server-sort order so it lands on page 1, where the row count
      // is asserted. A leftover client hide removes exactly this row.
      makeTx({
        id: 500,
        description: "Row 500",
        // Partner id 499 is absent from the corpus AND lower than 500, so this
        // leg survives as the transfer's only representative despite holding
        // the higher id.
        linked_transaction_id: 499,
        linked_account_name: ACCT_B.name,
        type: "income",
        account_id: ACCT_B.id,
        account_name: ACCT_B.name,
      }),
    ];
    for (let i = 1; i <= 25; i++) {
      rows.push(makeTx({ id: i, description: `Row ${i}`, amount: 10 + i }));
    }
    for (let p = 0; p < 4; p++) {
      const [lo, hi] = makePair(100 + p * 2, 101 + p * 2);
      rows.push(lo, hi);
    }
    return rows;
  }

  it("F1: a page of limit=25 renders exactly 25 rows when the corpus contains pairs", async () => {
    setupApiFetch(collapseCorpus());
    render(<TransactionsPage />);
    await waitForStableTxList();

    await waitFor(() => {
      expect(desktopRowCount()).toBe(25);
    });

    // The request must carry the opt-in, or the server never collapses.
    expect(listUrls().some((u) => u.includes("collapse_transfers=true"))).toBe(true);

    // Non-vacuity guard: the rendered page really does contain a row whose id
    // exceeds its linked_transaction_id, which is what a client-side hide
    // would have removed.
    expect(desktopRowIds()).toContain(500);
  });

  it("F2: a pair straddling the raw page boundary neither duplicates nor disappears", async () => {
    // 51 raw -> 50 collapsed. The pair's legs sit at RAW indices 24 and 25, so
    // without a server-side collapse one leg lands on page 1 and the other on
    // page 2.
    const rows: Tx[] = [];
    for (let i = 1; i <= 24; i++) {
      rows.push(makeTx({ id: i, description: `Row ${i}`, amount: 10 + i }));
    }
    const [lo, hi] = makePair(900, 901);
    rows.push(lo, hi);
    for (let i = 26; i <= 50; i++) {
      rows.push(makeTx({ id: i, description: `Row ${i}`, amount: 10 + i }));
    }
    setupApiFetch(rows);

    render(<TransactionsPage />);
    await waitForStableTxList();

    await waitFor(() => expect(desktopRowCount()).toBe(25));
    const page1 = desktopRowIds();

    fireEvent.click(
      await screen.findByLabelText("Next page", undefined, { timeout: 8000 }),
    );
    await waitFor(() => {
      expect(listUrls().some((u) => u.includes("offset=25"))).toBe(true);
    });
    await waitFor(() => expect(desktopRowCount()).toBe(25));
    const page2 = desktopRowIds();

    // No id on both pages...
    expect(page1.filter((id) => page2.includes(id))).toEqual([]);
    // ...and the union is the whole collapsed set, with no gaps.
    const union = new Set([...page1, ...page2]);
    expect(union.size).toBe(50);
    // The surviving leg appears exactly once across the two pages; the
    // partner never appears at all.
    expect([...page1, ...page2].filter((id) => id === 900)).toEqual([900]);
    expect(union.has(901)).toBe(false);
  });

  it("F3: Pagination reports the POST-collapse total", async () => {
    // 30 raw -> 24 collapsed (6 pairs + 18 singles). The numbers matter:
    // VACUITY TRAP — 30 raw / 26 collapsed would NOT work, because
    // ceil(26/25) === ceil(30/25) === 2 and the assertion would pass under the
    // broken implementation. 24 collapsed gives 1 page against 2.
    const rows: Tx[] = [];
    for (let i = 1; i <= 18; i++) {
      rows.push(makeTx({ id: i, description: `Row ${i}`, amount: 10 + i }));
    }
    for (let p = 0; p < 6; p++) {
      const [lo, hi] = makePair(200 + p * 2, 201 + p * 2);
      rows.push(lo, hi);
    }
    expect(rows.length).toBe(30);
    setupApiFetch(rows);

    render(<TransactionsPage />);
    await screen.findAllByText("Row 1", undefined, { timeout: 8000 });

    await waitFor(() => expect(desktopRowCount()).toBe(24));

    // The page mounts <Pagination> only when `total > pageSize || page > 0`.
    // 24 collapsed against a page size of 25 means NO pagination bar at all.
    // Under the broken (uncollapsed) implementation total would be 30 > 25,
    // so the bar would mount and read "Page 1 of 2" / "30 total" — which is
    // precisely the lie TBD-268 reported.
    expect(screen.queryByText(/Page 1 of/)).toBeNull();
    expect(screen.queryByText(/30 total/)).toBeNull();
    expect(screen.queryByLabelText(/per page/i)).toBeNull();
  });

  it("F5: select-all covers every rendered row", async () => {
    setupApiFetch(collapseCorpus());
    render(<TransactionsPage />);
    await waitForStableTxList();
    await waitFor(() => expect(desktopRowCount()).toBe(25));

    // Kills `selectableTxs` not being re-pointed at `transactions`: the old
    // filtered array would have excluded the higher-id survivors, so the bulk
    // bar would read 22, not 25.
    fireEvent.click(screen.getAllByLabelText("Select all on page")[0]);

    await waitFor(() => {
      expect(screen.getAllByText("25 selected").length).toBeGreaterThan(0);
    });
  });

  it("F6: a collapsed transfer renders source -> destination without the partner in the page", async () => {
    // VACUITY TRAP: exactly ONE row. If the fixture also carried the partner,
    // a leftover txMap lookup would resolve it and right/wrong would agree.
    const only = makeTx({
      id: 10,
      description: "Row 10",
      type: "expense",
      account_id: ACCT_A.id,
      account_name: "Checking",
      linked_transaction_id: 11,
      linked_account_name: "Savings",
    });
    setupApiFetch([only]);

    render(<TransactionsPage />);
    await screen.findAllByText("Row 10", undefined, { timeout: 8000 });

    await waitFor(() => {
      expect(screen.getAllByText(/Checking\s*→\s*Savings/).length).toBeGreaterThan(0);
    });
  });

  it("F7: the arrow reads source -> destination even when the INCOME leg survived", async () => {
    // The income leg is the destination, so it must render on the RIGHT of the
    // arrow regardless of which leg the collapse kept. Account names differ,
    // or the assertion could not tell the two orderings apart.
    const only = makeTx({
      id: 11,
      description: "Row 11",
      type: "income",
      account_id: ACCT_B.id,
      account_name: "Savings",
      linked_transaction_id: 10,
      linked_account_name: "Checking",
    });
    setupApiFetch([only]);

    render(<TransactionsPage />);
    await screen.findAllByText("Row 11", undefined, { timeout: 8000 });

    await waitFor(() => {
      expect(screen.getAllByText(/Checking\s*→\s*Savings/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/Savings\s*→\s*Checking/)).toBeNull();
  });

  it("F7b: a ONE-WAY reconciliation match is not dressed up as a transfer", async () => {
    // linked_transaction_id set, linked_account_name null => a reconcile match
    // per reconciliation_service._apply_match. It must render as a plain row
    // and must NOT offer Unlink, which would let unpair_transactions silently
    // recategorize the unrelated canonical row.
    const matched = makeTx({
      id: 42,
      description: "Row 42",
      account_name: "Checking",
      linked_transaction_id: 7,
      linked_account_name: null,
    });
    setupApiFetch([matched]);

    render(<TransactionsPage />);
    await screen.findAllByText("Row 42", undefined, { timeout: 8000 });

    await waitFor(() => {
      expect(screen.getAllByTestId("tx-row-desktop-42").length).toBe(1);
    });
    expect(screen.queryByRole("button", { name: /Unlink transfer: Row 42/i })).toBeNull();
    expect(screen.queryByText(/→/)).toBeNull();
  });
});
