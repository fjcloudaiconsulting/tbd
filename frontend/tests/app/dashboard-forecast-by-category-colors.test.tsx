import { render, screen, waitFor } from "@testing-library/react";

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

// Lightweight recharts mock that renders <Bar> children so <Cell> elements
// land in the DOM with their fill attribute. The dashboard's Forecast by
// Category bar passes a list of Cell children whose fills encode the
// over/under-plan classification, that's what this test asserts.
vi.mock("recharts", () => {
  return {
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
    BarChart: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="bar-chart">{children}</div>
    ),
    Bar: ({
      dataKey,
      fill,
      children,
    }: {
      dataKey?: string;
      fill?: string;
      children?: React.ReactNode;
    }) => (
      <div data-testid={`bar-${dataKey}`} data-fill={fill}>
        {children}
      </div>
    ),
    Cell: ({ fill }: { fill?: string }) => (
      <div data-testid="cell" data-fill={fill} />
    ),
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
    PieChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    Pie: () => null,
  };
});

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

// Plan with two expense lines:
// - Housing: planned 20.00, actual 39.50 (OVER plan, +19.50) -> red Cell
// - Groceries: planned 500.00, actual 200.00 (UNDER plan) -> green Cell
// Mirrors the user-reported repro on 2026-05-10.
const PLAN_OVER_AND_UNDER = {
  id: 1,
  billing_period_id: 1,
  period_start: "2026-05-01",
  period_end: null,
  status: "active",
  total_planned_income: "0",
  total_planned_expense: "520.00",
  total_actual_income: "0",
  total_actual_expense: "239.50",
  items: [
    {
      id: 1,
      plan_id: 1,
      category_id: 100,
      category_name: "Housing",
      parent_id: null,
      type: "expense",
      planned_amount: "20.00",
      source: "manual",
      actual_amount: "39.50",
      variance: "19.50",
    },
    {
      id: 2,
      plan_id: 1,
      category_id: 101,
      category_name: "Groceries",
      parent_id: null,
      type: "expense",
      planned_amount: "500.00",
      source: "manual",
      actual_amount: "200.00",
      variance: "-300.00",
    },
  ],
};

function setupApiMocks() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?"))
      return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/forecast-plans/current"))
      return Promise.resolve(PLAN_OVER_AND_UNDER);
    if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
    if (url.startsWith("/api/v1/forecast/account-balances"))
      return Promise.resolve({ accounts: [], period_start: "2026-05-01", period_end: "2026-05-31" });
    if (url.startsWith("/api/v1/transactions?status=pending"))
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    if (url.startsWith("/api/v1/transactions"))
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    return Promise.resolve(null);
  }) as never);
}

describe("DashboardPage - Forecast by Category over/under-plan colors", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    setupApiMocks();
  });

  it("paints actual-bar Cells red when actual > planned and green otherwise", async () => {
    render(<DashboardPage />);

    // Wait for the Forecast by Category card to render (it's gated on
    // forecast?.items being non-empty).
    await waitFor(() => {
      expect(screen.getByText("Forecast by Category")).toBeTruthy();
    });

    // The actual <Bar dataKey="actual"> in the Forecast by Category chart
    // wraps the per-row <Cell> children with the over/under-plan fills.
    // There's only one chart on the page that mounts a Bar with
    // dataKey="actual" today (Forecast by Category). Find it by walking
    // every actual-bar and picking the one whose Cell count matches the
    // expense-line count (2 in this fixture).
    const allActualBars = await screen.findAllByTestId("bar-actual");
    const forecastBar = allActualBars.find(
      (b) => b.querySelectorAll('[data-testid="cell"]').length === 2,
    );
    expect(forecastBar).toBeTruthy();

    const cells = forecastBar!.querySelectorAll('[data-testid="cell"]');
    expect(cells.length).toBe(2);

    // Order in the chart data follows the order of expenseItems passed
    // into the chart, which mirrors plan.items: Housing then Groceries.
    // Housing: actual 39.50 > planned 20 -> over (red, var(--color-danger))
    expect(cells[0].getAttribute("data-fill")).toBe("var(--color-danger)");
    // Groceries: actual 200 < planned 500 -> under (green, var(--color-success))
    expect(cells[1].getAttribute("data-fill")).toBe("var(--color-success)");
  });

  it("legend lists Planned, Under plan, and Over plan", async () => {
    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Forecast by Category")).toBeTruthy();
    });

    expect(screen.getByText("Planned")).toBeTruthy();
    expect(screen.getByText("Under plan")).toBeTruthy();
    expect(screen.getByText("Over plan")).toBeTruthy();
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// LegacyDashboard — show ALL categories (no per-category cap)
//
// LegacyDashboard (flag-gated rollback) carries its OWN dashBudgets /
// forecastChartRows memos. PR #532 un-sliced them (and dropped the fixed
// per-count inline heights). These tests seed 10 budget + 10 forecast
// categories and assert every one reaches its chart (one <Cell> per row), so
// re-introducing a slice on the legacy page fails CI instead of passing
// silently. DashboardPage renders LegacyDashboard here because the mocked
// useAuth returns no `features.customDashboard`.
// ══════════════════════════════════════════════════════════════════════════════

// 10 forecast expense lines, all UNDER plan (fill classification is covered by
// the tests above; here we only count rows).
const PLAN_TEN_EXPENSE_LINES = {
  id: 2,
  billing_period_id: 1,
  period_start: "2026-05-01",
  period_end: null,
  status: "active",
  total_planned_income: "0",
  total_planned_expense: "5000.00",
  total_actual_income: "0",
  total_actual_expense: "2000.00",
  items: Array.from({ length: 10 }, (_, i) => ({
    id: i + 1,
    plan_id: 2,
    category_id: 200 + i,
    category_name: `Forecast Cat ${i}`,
    parent_id: null,
    type: "expense",
    planned_amount: "500.00",
    source: "manual",
    actual_amount: "200.00",
    variance: "-300.00",
  })),
};

// 10 budgets, one per category.
const TEN_BUDGETS = Array.from({ length: 10 }, (_, i) => ({
  id: i + 1,
  category_id: 300 + i,
  category_name: `Budget Cat ${i}`,
  amount: "500.00",
  spent: "300.00",
  remaining: "200.00",
  percent_used: 60,
  period_start: "2026-05-01",
  period_end: null,
}));

describe("LegacyDashboard - shows all categories (no per-category cap)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
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

  it("Forecast by Category renders a Cell for every expense line when seeded with 10", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url === "/api/v1/categories") return Promise.resolve([]);
      if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?"))
        return Promise.resolve([]);
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings/billing-periods")
        return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
      if (url.startsWith("/api/v1/forecast-plans/current"))
        return Promise.resolve(PLAN_TEN_EXPENSE_LINES);
      if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast/account-balances"))
        return Promise.resolve({ accounts: [], period_start: "2026-05-01", period_end: "2026-05-31" });
      if (url.startsWith("/api/v1/transactions"))
        return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve(null);
    }) as never);

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Forecast by Category")).toBeTruthy();
    });

    // The actual <Bar dataKey="actual"> wraps one <Cell> per forecast row.
    // With budgets empty there is no budget spent-bar, so the only bar-actual
    // is the forecast one — assert it carries all 10 cells.
    const actualBars = await screen.findAllByTestId("bar-actual");
    const forecastBar = actualBars.find(
      (b) => b.querySelectorAll('[data-testid="cell"]').length === 10,
    );
    expect(forecastBar).toBeTruthy();
    expect(forecastBar!.querySelectorAll('[data-testid="cell"]')).toHaveLength(10);
  });

  it("Budget Progress renders a Cell for every budget when seeded with 10", async () => {
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url === "/api/v1/categories") return Promise.resolve([]);
      if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?"))
        return Promise.resolve(TEN_BUDGETS);
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings/billing-periods")
        return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
      // No forecast plan → no forecast actual-bar to disambiguate against.
      if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast/account-balances"))
        return Promise.resolve({ accounts: [], period_start: "2026-05-01", period_end: "2026-05-31" });
      if (url.startsWith("/api/v1/transactions"))
        return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      return Promise.resolve(null);
    }) as never);

    render(<DashboardPage />);

    await waitFor(() => {
      expect(screen.getByText("Budget Progress")).toBeTruthy();
    });

    // The spent <Bar dataKey="spent"> wraps one <Cell> per budget row.
    const spentBars = await screen.findAllByTestId("bar-spent");
    const budgetBar = spentBars.find(
      (b) => b.querySelectorAll('[data-testid="cell"]').length === 10,
    );
    expect(budgetBar).toBeTruthy();
    expect(budgetBar!.querySelectorAll('[data-testid="cell"]')).toHaveLength(10);
  });
});
