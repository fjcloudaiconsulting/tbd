import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { SORT_KEY_DASHBOARD_SPENDING } from "@/lib/hooks/persisted-keys";

// Item 16 (D2 Dashboard Spending sortable card) regression coverage. The
// Spending by Category card now has Category | % | Amount headers; sort
// state persists to localStorage under SORT_KEY_DASHBOARD_SPENDING.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/components/dashboard/AccountTile", () => ({
  default: () => <div data-testid="account-tiles" />,
}));

vi.mock("@/components/dashboard/AccountMonthEndForecast", () => ({
  default: () => <div data-testid="account-month-end" />,
}));

vi.mock("@/components/dashboard/OnTrackTile", () => ({
  default: () => <div data-testid="on-track" />,
}));

vi.mock("recharts", () => ({
  PieChart: ({ children }: { children: React.ReactNode }) => <div data-testid="pie">{children}</div>,
  Pie: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Cell: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
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

const ACCT = {
  id: 100, name: "Checking", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: "0.00", currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CAT_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

const CAT_RENT = {
  id: 12, name: "Rent", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "rent", is_system: false, transaction_count: 0,
};

function tx(overrides: Record<string, unknown>) {
  return {
    id: 1, account_id: 100, account_name: "Checking",
    category_id: 11, category_name: "Groceries",
    description: "Test", amount: "10.00", type: "expense",
    status: "settled", date: "2026-05-01",
    is_recurring: false, recurring_template_id: null,
    linked_transaction_id: null, related_id: null,
    metadata_json: null, created_by_user_id: 1,
    ...overrides,
  };
}

function setupApiFetch() {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CAT_GROCERIES, CAT_RENT] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) {
      return [{ id: 1, start_date: "2026-05-01", end_date: null }] as never;
    }
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    if (url.startsWith("/api/v1/forecast")) return null as never;
    if (url.startsWith("/api/v1/transactions")) {
      const items = [
        tx({ id: 1, amount: "100.00", category_id: 11, category_name: "Groceries", date: "2026-05-02" }),
        tx({ id: 2, amount: "500.00", category_id: 12, category_name: "Rent", date: "2026-05-03" }),
        tx({ id: 3, amount: "50.00", category_id: 11, category_name: "Groceries", date: "2026-05-04" }),
      ];
      return { items, total: items.length, limit: 200, offset: 0 } as never;
    }
    return null as never;
  });
  return apiFetchMock;
}

async function awaitSpendingHeaders() {
  await waitFor(() => {
    expect(
      screen.queryByRole("button", { name: /Sort by category/i }),
    ).toBeInTheDocument();
  });
}

describe("DashboardPage - Spending by Category sort persistence (item 16)", () => {
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

  afterEach(() => {
    cleanup();
  });

  it("renders sortable Category | % | Amount headers", async () => {
    setupApiFetch();
    render(<DashboardPage />);
    await awaitSpendingHeaders();

    expect(screen.getByRole("button", { name: /Sort by category/i }))
      .toHaveTextContent(/Category/);
    expect(screen.getByRole("button", { name: /Sort by percent of total/i }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sort by amount/i }))
      .toBeInTheDocument();
  });

  it("clicking a column header writes the sort to localStorage", async () => {
    setupApiFetch();
    render(<DashboardPage />);
    await awaitSpendingHeaders();

    fireEvent.click(screen.getByRole("button", { name: /Sort by category/i }));

    await waitFor(() => {
      const stored = window.localStorage.getItem(SORT_KEY_DASHBOARD_SPENDING);
      expect(stored).not.toBeNull();
      expect(JSON.parse(stored!)).toEqual({ field: "name", dir: "asc" });
    });
  });

  it("toggles direction on same-column click", async () => {
    setupApiFetch();
    render(<DashboardPage />);
    await awaitSpendingHeaders();

    const catBtn = screen.getByRole("button", { name: /Sort by category/i });
    const catHeader = catBtn.closest('[role="columnheader"]') as HTMLElement;
    fireEvent.click(catBtn);
    await waitFor(() =>
      expect(catHeader.getAttribute("aria-sort")).toBe("ascending"),
    );

    fireEvent.click(catBtn);
    await waitFor(() =>
      expect(catHeader.getAttribute("aria-sort")).toBe("descending"),
    );

    const stored = JSON.parse(
      window.localStorage.getItem(SORT_KEY_DASHBOARD_SPENDING)!,
    );
    expect(stored).toEqual({ field: "name", dir: "desc" });
  });

  it("rehydrates the spending sort from localStorage on mount", async () => {
    window.localStorage.setItem(
      SORT_KEY_DASHBOARD_SPENDING,
      JSON.stringify({ field: "percent", dir: "asc" }),
    );
    setupApiFetch();
    render(<DashboardPage />);
    await awaitSpendingHeaders();

    const pctBtn = screen.getByRole("button", { name: /Sort by percent of total/i });
    const pctHeader = pctBtn.closest('[role="columnheader"]') as HTMLElement;
    expect(pctHeader.getAttribute("aria-sort")).toBe("ascending");
    // Other columns should report aria-sort="none".
    const catBtn = screen.getByRole("button", { name: /Sort by category/i });
    const catHeader = catBtn.closest('[role="columnheader"]') as HTMLElement;
    expect(catHeader.getAttribute("aria-sort")).toBe("none");
  });

  it("renders a lucide chevron icon for the active spending sort column", async () => {
    setupApiFetch();
    render(<DashboardPage />);
    await awaitSpendingHeaders();

    // Default is amount-desc, so the Amount header should carry aria-sort="descending"
    // and render the down chevron (ChevronDown). Other columns render the
    // ChevronsUpDown unsorted indicator.
    const amtBtn = screen.getByRole("button", { name: /Sort by amount/i });
    const amtHeader = amtBtn.closest('[role="columnheader"]') as HTMLElement;
    expect(amtHeader.getAttribute("aria-sort")).toBe("descending");
    expect(amtBtn.querySelector("svg")).not.toBeNull();
  });
});
