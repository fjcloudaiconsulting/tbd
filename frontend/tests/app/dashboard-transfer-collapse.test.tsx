/**
 * TBD-268 fences F8 / F9 — both dashboard surfaces.
 *
 * Both the legacy dashboard page and the canvas RecentTransactionsWidget
 * carried the same client-side "hide the higher-id leg" rule that TBD-268 is
 * about. With the collapse moved server-side, that rule must be GONE from both
 * or a full page of `PAGE_SIZE` rows renders short again.
 *
 * Row counting uses the per-row `dash-settled-{id}` test id, which exists on
 * both surfaces. The bulk of the dashboard also renders account/category
 * strips, so text-based counting would over-match.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Recharts' ResponsiveContainer measures its parent, and jsdom reports 0x0 —
// so every chart in the page renders NOTHING and no bar can be clicked. F8d
// needs a real click on the Budget Progress bar (the only production path that
// sets a chart filter naming a category with no matching transaction), so give
// the container a fixed size. Measured: without this stub the page renders 0
// `.recharts-bar-rectangle` elements; with it, 2.
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) => (
      <div style={{ width: 400, height: 300 }}>
        {React.cloneElement(children, { width: 400, height: 300 } as never)}
      </div>
    ),
  };
});

import DashboardPage from "@/app/dashboard/page";
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
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Acme",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

function tx(over: Record<string, unknown> = {}) {
  return {
    id: 1, account_id: 10, amount: "12.00", type: "expense", status: "settled",
    date: "2026-05-10", description: "Row", category_id: 1,
    category_name: "Groceries", account_name: "Checking", currency: "EUR",
    linked_transaction_id: null, linked_account_name: null,
    is_imported: false, is_manual_adjustment: false, settled_date: "2026-05-11",
    tags: [],
    ...over,
  };
}

// The legacy dashboard's PAGE_SIZE is 10. A collapsed page therefore holds 10
// rows, two of which are transfers whose partner is NOT in the page — exactly
// the shape a leftover client hide would shrink to 8.
function collapsedPage() {
  const rows = [];
  for (let i = 1; i <= 8; i++) {
    rows.push(tx({ id: i, description: `Row ${i}` }));
  }
  // Both survivors hold a HIGHER id than their partner, so the old
  // `id > linked_transaction_id` rule removes both.
  rows.push(tx({
    id: 101, description: "Transfer A", type: "income",
    account_name: "Savings", linked_transaction_id: 100,
    linked_account_name: "Checking",
  }));
  rows.push(tx({
    id: 103, description: "Transfer B", type: "income",
    account_name: "Savings", linked_transaction_id: 102,
    linked_account_name: "Checking",
  }));
  return rows;
}

let listUrls: string[] = [];

function mockDashboard(
  rows: ReturnType<typeof tx>[],
  budgets: Record<string, unknown>[] = [],
) {
  listUrls = [];
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?")) return Promise.resolve(budgets);
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
    // Record EVERY transactions-list URL, the all-time pending fetch included.
    // The push used to sit AFTER the status=pending early return, which made
    // F8b's "the pending fetch must not opt in" assertion true by mock routing
    // rather than by the code under test — it stayed green with the flag added
    // to fetchAll. Recording first is what gives that assertion teeth.
    if (url.startsWith("/api/v1/transactions")) {
      listUrls.push(url);
      if (url.startsWith("/api/v1/transactions?status=pending"))
        return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve({ items: rows, total: rows.length, limit: 200, offset: 0 });
    }
    return Promise.resolve({});
  }) as never);
}

function rowCount(): number {
  return screen.getAllByTestId(/^dash-settled-\d+$/).length;
}

describe("Dashboard — transfer collapse (TBD-268 F8)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    window.localStorage.clear();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("F8: a collapsed page of 10 renders 10 rows, transfers included", async () => {
    mockDashboard(collapsedPage());
    render(<DashboardPage />);

    await screen.findByTestId("dash-settled-1");
    await waitFor(() => expect(rowCount()).toBe(10));

    // Kills the legacy hide surviving: it would drop ids 101 and 103.
    const ids = screen
      .getAllByTestId(/^dash-settled-\d+$/)
      .map((el) => Number(el.getAttribute("data-testid")!.replace("dash-settled-", "")));
    expect(ids).toContain(101);
    expect(ids).toContain(103);
  });

  it("F8b: both the page fetch and the limit=200 snapshot opt in", async () => {
    mockDashboard(collapsedPage());
    render(<DashboardPage />);
    await screen.findByTestId("dash-settled-1");

    // The snapshot is not a one-shot sum source — under a chart filter it
    // becomes the rendered source, so it must collapse alongside the page.
    await waitFor(() => {
      expect(listUrls.some((u) => u.includes("limit=10") && u.includes("collapse_transfers=true"))).toBe(true);
      expect(listUrls.some((u) => u.includes("limit=200") && u.includes("collapse_transfers=true"))).toBe(true);
    });
    // ...but the all-time pending fetch must NOT: each leg of a transfer sits
    // on a different account, so collapsing it would zero an account's pending.
    // Asserted as "the pending URLs that WERE issued carry no flag", with an
    // explicit non-vacuity guard that at least one was issued — the previous
    // shape ("no pending URL was recorded at all") was satisfied by the mock's
    // own routing and could never fail.
    const pending = listUrls.filter((u) => u.includes("status=pending"));
    expect(pending.length).toBeGreaterThan(0);
    pending.forEach((u) => expect(u).not.toContain("collapse_transfers"));
  });

  it("F8c: the transfer subline reads source -> destination on a surviving income leg", async () => {
    mockDashboard(collapsedPage());
    render(<DashboardPage />);
    await screen.findByTestId("dash-settled-101");

    await waitFor(() => {
      expect(screen.getAllByText(/Checking\s*→\s*Savings/).length).toBeGreaterThan(0);
    });
    expect(screen.queryByText(/Savings\s*→\s*Checking/)).toBeNull();
  });

  it("F8d: a chart filter that empties the RENDERED list still shows the empty state", async () => {
    // VACUITY TRAP this fence was rewritten to avoid: the original passed
    // `mockDashboard([])`, so `transactions.length === 0` and
    // `sortedVisibleTxs.length === 0` AGREED and reverting the empty state to
    // the raw page array stayed green against the whole suite.
    //
    // The two disagree exactly when a chart filter names a category with no
    // matching transaction. The production path is the Budget Progress bar:
    // a budget can exist for a category nothing was spent on this period.
    // Clicking it sets chartFilter to that category, so `visibleTxs` (the
    // snapshot) filters down to nothing while `transactions` still holds a
    // full page — a blank card under the old keying.
    const rows = [];
    for (let i = 1; i <= 4; i++) rows.push(tx({ id: i, description: `Row ${i}` }));
    mockDashboard(rows, [
      {
        id: 1, category_id: 9, category_name: "Rent",
        amount: "500.00", spent: "300.00", percent_used: 60,
        period_start: "2026-05-01", period_end: null,
      },
    ]);
    const { container } = render(<DashboardPage />);

    // Pre-state: rows render and there is NO empty state.
    await screen.findByTestId("dash-settled-1");
    await waitFor(() => expect(rowCount()).toBe(4));
    // The tile's empty state renders one of two strings depending on `canAdd`;
    // this fence is about the BLOCK being present, not which copy it shows.
    const EMPTY = /No transactions this period|Create accounts and categories first/;
    expect(screen.queryByText(EMPTY)).toBeNull();

    const bars = container.querySelectorAll(".recharts-bar-rectangle");
    expect(bars.length).toBeGreaterThan(0);
    fireEvent.click(bars[0]);

    await waitFor(() => {
      expect(screen.getByText(/Filtering: Rent/)).toBeInTheDocument();
    });
    // The rendered list is now empty while the raw page array still holds 4.
    expect(screen.queryAllByTestId(/^dash-settled-\d+$/)).toHaveLength(0);
    expect(screen.getByText(EMPTY)).toBeInTheDocument();
  });
});
