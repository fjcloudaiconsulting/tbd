import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const TXS = [
  {
    id: 1,
    account_id: 10,
    amount: "12.00",
    type: "expense",
    status: "settled",
    date: "2026-05-03",
    description: "Apples",
    category_id: 1,
    category_name: "Groceries",
    account_name: "Checking",
    currency: "EUR",
    linked_transaction_id: null,
    is_imported: false,
    settled_date: "2026-05-03",
  },
  {
    id: 2,
    account_id: 10,
    amount: "50.00",
    type: "expense",
    status: "pending",
    date: "2026-05-02",
    description: "Bananas",
    category_id: 1,
    category_name: "Groceries",
    account_name: "Credit",
    currency: "EUR",
    linked_transaction_id: null,
    is_imported: false,
    settled_date: null,
  },
];

function mockDashboard() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?")) return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/transactions?status=pending")) return Promise.resolve([TXS[1]]);
    if (url.startsWith("/api/v1/transactions")) return Promise.resolve(TXS);
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage Recent Transactions columns", () => {
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
    mockDashboard();
  });

  function getRecentTxHeader(container: HTMLElement, name: string): HTMLElement {
    // Recent Transactions mini-header is the only Dashboard element where
    // Date/Description/Status/Amount appear as a 12-col grid of <button>s
    // directly under a `hidden sm:block` wrapper. Locate that wrapper, then
    // pick the column header whose textContent starts with the given label.
    // This isolates us from the Spending card's "Amount" header (which uses
    // aria-label="Sort by amount") and from any unrelated dashboard buttons.
    const grids = container.querySelectorAll(".hidden.sm\\:block .grid.grid-cols-12");
    for (const grid of Array.from(grids)) {
      const btns = grid.querySelectorAll(":scope > button");
      for (const b of Array.from(btns)) {
        const txt = b.textContent ?? "";
        if (txt.startsWith(name)) return b as HTMLElement;
      }
    }
    throw new Error(`No Recent Tx sort header for "${name}"`);
  }

  async function findHeaderButton(name: string, container?: HTMLElement) {
    return await waitFor(() => {
      const root = container ?? document.body;
      return getRecentTxHeader(root, name);
    });
  }

  it("renders sort headers in /transactions order: Date, Description, Status, Amount", async () => {
    const { container } = render(<DashboardPage />);

    // The mini-header lives in a hidden sm:block; jsdom layouts ignore
    // breakpoints so the header still renders in the DOM and is queryable.
    const dateBtn = await findHeaderButton("Date", container);
    const descBtn = await findHeaderButton("Description", container);
    const statusBtn = await findHeaderButton("Status", container);
    const amountBtn = await findHeaderButton("Amount", container);

    // Confirm document order matches column order left -> right.
    const ordered = [dateBtn, descBtn, statusBtn, amountBtn];
    for (let i = 0; i < ordered.length - 1; i++) {
      const rel = ordered[i].compareDocumentPosition(ordered[i + 1]);
      expect(rel & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }
  });

  it("clicking Status header toggles sort and shows arrow indicator", async () => {
    const { container } = render(<DashboardPage />);

    const statusBtn = await findHeaderButton("Status", container);

    // First click: ascending (alphabetical -> pending before settled).
    fireEvent.click(statusBtn);
    await waitFor(() => {
      expect(getRecentTxHeader(container, "Status").textContent ?? "").toMatch(
        /Status\s+↑/,
      );
    });

    // Second click on the same header flips to descending.
    fireEvent.click(getRecentTxHeader(container, "Status"));
    await waitFor(() => {
      expect(getRecentTxHeader(container, "Status").textContent ?? "").toMatch(
        /Status\s+↓/,
      );
    });
  });

  it("default sort is Date desc; status-asc places pending row above settled", async () => {
    const { container } = render(<DashboardPage />);

    // Wait for both rows to render. Each tx row is a single responsive
    // wrapper (flex on mobile, grid-cols-12 on sm+); we read row textContent
    // off the wrapper directly.
    const desktopRows = () =>
      Array.from(
        container.querySelectorAll(".flex.flex-col.sm\\:grid.sm\\:grid-cols-12"),
      );
    const rowText = (idx: number) =>
      desktopRows()[idx]?.textContent ?? "";

    await waitFor(() => {
      expect(desktopRows().length).toBeGreaterThanOrEqual(2);
      expect(rowText(0)).toContain("Apples");
      expect(rowText(1)).toContain("Bananas");
    });

    // Default: date desc -> 2026-05-03 ("Apples") above 2026-05-02 ("Bananas").
    expect(rowText(0)).toContain("Apples");
    expect(rowText(1)).toContain("Bananas");

    // Switch to status asc -> "pending" ("Bananas") above "settled" ("Apples").
    const statusBtn = await findHeaderButton("Status", container);
    fireEvent.click(statusBtn);
    await waitFor(() => {
      expect(getRecentTxHeader(container, "Status").textContent ?? "").toMatch(
        /Status\s+↑/,
      );
    });

    await waitFor(() => {
      expect(rowText(0)).toContain("Bananas");
      expect(rowText(1)).toContain("Apples");
    });
  });

  it("uses responsive grid: 12-col on sm+, flex two-line on mobile", async () => {
    const { container } = render(<DashboardPage />);

    await waitFor(() => {
      // Each transaction row wraps in a `flex flex-col sm:grid sm:grid-cols-12`
      // container — one node per tx, no duplicated subtrees.
      expect(
        container.querySelectorAll(".flex.flex-col.sm\\:grid.sm\\:grid-cols-12")
          .length,
      ).toBeGreaterThanOrEqual(2);
    });

    // Header row is `hidden sm:block` to disappear under sm; the row wrapper
    // is always present (the breakpoint flips layout, not visibility).
    const header = container.querySelector(".hidden.sm\\:block .grid.grid-cols-12");
    expect(header).toBeTruthy();

    // Confirm Status pill + Amount share a single flex container on mobile
    // and split to columns on sm+ via Tailwind's `sm:contents`.
    const mobileSplit = container.querySelectorAll(".flex.items-center.justify-between.sm\\:contents");
    expect(mobileSplit.length).toBeGreaterThan(0);

    // Right-aligned tabular amount must exist (rightmost cell on sm+).
    const amountSpans = container.querySelectorAll(".sm\\:text-right .tabular-nums");
    expect(amountSpans.length).toBeGreaterThan(0);
  });
});
