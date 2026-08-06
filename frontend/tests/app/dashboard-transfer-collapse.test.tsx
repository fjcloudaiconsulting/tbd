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
let projectionCategories: Record<string, unknown>[] = [];

function mockDashboard(
  rows: ReturnType<typeof tx>[],
  budgets: Record<string, unknown>[] = [],
  categories: Record<string, unknown>[] = [],
) {
  listUrls = [];
  projectionCategories = categories;
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
    // TBD-221: the Spending donut and the chart-filter drilldown both read the
    // UNGATED spending-by-category rollup (and its window), so it can no
    // longer be null.
    //
    // ⚠ This branch MUST precede the generic /api/v1/transactions branch below
    // — the rollup lives on the transactions router and shares its prefix. It
    // must also stay OUT of `listUrls`, which exists to fence the shape of the
    // LIST queries; a rollup URL in there would make F8b's assertions read
    // against a URL that is not a list query at all.
    if (url.startsWith("/api/v1/transactions/spending-by-category"))
      return Promise.resolve({
        period_start: "2026-05-01",
        period_end: "2026-05-31",
        executed_expense: "0",
        categories: projectionCategories,
      });
    // Record EVERY transactions-list URL, the all-time pending fetch included.
    // The push used to sit AFTER the status=pending early return, which made
    // F8b's "the pending fetch must not opt in" assertion true by mock routing
    // rather than by the code under test — it stayed green with the flag added
    // to fetchAll. Recording first is what gives that assertion teeth.
    if (url.startsWith("/api/v1/transactions")) {
      listUrls.push(url);
      if (url.startsWith("/api/v1/transactions?status=pending"))
        return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      // A drilldown into a category with no settled spend this period.
      if (url.includes("category_id="))
        return Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 });
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

  it("F8b: the unfiltered page fetch opts in; the deleted snapshot is not issued at all", async () => {
    mockDashboard(collapsedPage());
    render(<DashboardPage />);
    await screen.findByTestId("dash-settled-1");

    // ⚠ INVERTED by TBD-221. This fence used to require the `limit=200`
    // snapshot to carry collapse_transfers=true, on the grounds that under a
    // chart filter the snapshot BECAME the rendered source. That coupling is
    // exactly what this ticket removed: the filtered list is a server query
    // now, so the snapshot has no consumer and is gone. The unfiltered page
    // still collapses.
    await waitFor(() => {
      expect(listUrls.some((u) => u.includes("limit=10") && u.includes("collapse_transfers=true"))).toBe(true);
    });
    expect(listUrls.filter((u) => u.startsWith("/api/v1/transactions?limit=200"))).toEqual([]);
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

  it("F8d: a Budget bar drilldown re-queries the server and renders ITS answer, empty state included", async () => {
    // ⚠ REWRITTEN by TBD-221. The original property — "the rendered list can
    // be empty while the raw page array is full, so key the empty state off
    // the RENDERED list" — is unreachable now: `sortedVisibleTxs` IS the page
    // the server returned, so the two arrays cannot disagree. What replaced
    // it is the reason they cannot: clicking a chart bar changes the QUERY.
    //
    // Same production path as before — a budget on a category nothing was
    // spent on this period. The server answers that drilldown with zero rows
    // while the unfiltered page still holds four.
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

    // The badge label is looked up from data already in memory — here the
    // budgets list, since Rent has no rollup row this period.
    await waitFor(() => {
      expect(screen.getByText(/Filtering: Rent/)).toBeInTheDocument();
    });
    // The drilldown carries the rollup's grouping and the rollup's window.
    await waitFor(() => {
      const drill = listUrls.find((u) => u.includes("category_id=9"));
      expect(drill).toBeTruthy();
      expect(drill).toContain("category_match=exact");
      expect(drill).toContain("reportable=true");
      expect(drill).not.toContain("collapse_transfers");
    });
    // …and the server's answer for that slice is what renders.
    await waitFor(() =>
      expect(screen.queryAllByTestId(/^dash-settled-\d+$/)).toHaveLength(0),
    );
    expect(screen.getByText(EMPTY)).toBeInTheDocument();
  });
});
