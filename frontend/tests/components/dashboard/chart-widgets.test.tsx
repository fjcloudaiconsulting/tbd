/**
 * Tests for the 3 Phase-2b chart widget tiles:
 *   - SpendingDonutWidget
 *   - BudgetBarsWidget
 *   - ForecastBarsWidget
 *
 * Also covers the 3 new entries in emptyDashboardWidget and the 3 new
 * arms in renderDashboardWidget.
 *
 * Mock strategy:
 *   - useDashboard is mocked at the module boundary (same pattern as
 *     dashboard-widget-registry.test.tsx).
 *   - SeriesTooltip and BudgetSpentBarShape are mocked to lightweight
 *     stubs (they have their own test files).
 *   - recharts ResponsiveContainer is replaced with a plain <div> wrapper
 *     so jsdom does not complain about zero-dimension SVG containers.
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";

import {
  emptyDashboardWidget,
  type DashboardWidgetType,
} from "@/lib/dashboard/widget-types";
import { renderDashboardWidget } from "@/components/dashboard/renderDashboardWidget";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";
import type { Transaction } from "@/lib/types";

// ── Lightweight chart-lib stubs ───────────────────────────────────────────────

vi.mock("@/components/charts/SeriesTooltip", () => ({
  SeriesTooltip: () => null,
}));

vi.mock("@/lib/chart-shapes", () => ({
  BudgetSpentBarShape: () => null,
}));

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="responsive-container">{children}</div>
    ),
    // BarChart must pass children through so the stubbed Bar components render.
    // The real BarChart processes children internally (via React.Children) and
    // never outputs the children's JSX directly — so we stub it to a plain div.
    BarChart: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="bar-chart">{children}</div>
    ),
    // Stub Bar so its onClick prop is invokable via a data-testid button.
    // The real recharts Bar renders SVG in a canvas that jsdom can't size,
    // so onClick is never reachable from tests without this stub.
    Bar: ({
      dataKey,
      onClick,
    }: {
      dataKey?: string;
      onClick?: (data: unknown, index: number) => void;
      children?: React.ReactNode;
    }) => (
      <button
        data-testid={`bar-${dataKey ?? "unknown"}`}
        onClick={() => onClick?.({}, 0)}
      />
    ),
  };
});

// ── Mock DashboardDataProvider ────────────────────────────────────────────────

const MOCK_SETFILTER = vi.fn();
const MOCK_TOGGLE_SORT = vi.fn();

const MOCK_SPENDING_SORT = {
  field: "amount" as const,
  dir: "desc" as const,
  setSort: vi.fn(),
  reset: vi.fn(),
  isDefault: true,
};

const MOCK_DASHBOARD_DATA: DashboardData = {
  accounts: [],
  activeAccounts: [],
  pendingByAccount: {},
  forecast: null,
  forecastProjection: null,
  projectionFailed: false,
  projectionLoading: false,
  onRetryProjection: vi.fn(),
  accountMonthEndForecast: null,
  accountMonthEndForecastError: false,
  periods: [],
  periodIdx: 0,
  setPeriodIdx: vi.fn(),
  selectedPeriod: null,
  isCurrentSelectedPeriod: true,
  isPastSelectedPeriod: false,
  isFutureSelectedPeriod: false,
  monthFrom: "2026-06-01",
  monthTo: "2026-06-30",
  jumpToCurrentPeriod: vi.fn(),
  allTransactions: [],
  budgets: [],
  dashBudgets: [],
  budgetChartData: [],
  donutData: [],
  totalSpend: 0,
  sortedSpending: [],
  spendingSort: MOCK_SPENDING_SORT,
  toggleSpendingSort: MOCK_TOGGLE_SORT,
  forecastExpenseItems: [],
  forecastChartRows: [],
  chartFilter: null,
  setChartFilter: MOCK_SETFILTER,
  transactions: [],
  txTotal: 0,
  page: 0,
  setPage: vi.fn(),
  pageSize: 10,
  visibleTxs: [],
  sortedVisibleTxs: [],
  txMap: new Map(),
  dashSort: {
    field: "date" as const,
    dir: "desc" as const,
    setSort: vi.fn(),
    reset: vi.fn(),
    isDefault: true,
  },
  toggleDashSort: vi.fn(),
  canAdd: true,
  onToggleTransactionStatus: vi.fn(),
  loading: false,
  error: null,
  refresh: vi.fn(),
};

vi.mock("@/components/dashboard/DashboardDataProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/dashboard/DashboardDataProvider")
  >("@/components/dashboard/DashboardDataProvider");
  return {
    ...actual,
    useDashboard: vi.fn(() => MOCK_DASHBOARD_DATA),
  };
});

// Also mock the reports widget components to avoid SWR/API wiring in
// renderDashboardWidget's fall-through branch (same as registry test).
vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: () => <div data-testid="kpi-widget-stub">KPIWidget</div>,
}));
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: () => <div data-testid="bar-widget-stub">BarWidget</div>,
}));

// ── Helpers ───────────────────────────────────────────────────────────────────

function mockWith(overrides: Partial<DashboardData>) {
  vi.mocked(useDashboard).mockReturnValueOnce({
    ...MOCK_DASHBOARD_DATA,
    ...overrides,
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// SpendingDonutWidget
// ══════════════════════════════════════════════════════════════════════════════

describe("SpendingDonutWidget", () => {
  it("renders 'Spending by Category' heading", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    expect(screen.getByText("Spending by Category")).toBeInTheDocument();
  });

  it("renders empty state 'No expense data yet' when donutData is empty", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    expect(screen.getByText("No expense data yet")).toBeInTheDocument();
  });

  it("renders chart content when donutData has items", () => {
    mockWith({
      donutData: [{ name: "Food", value: 200 }],
      sortedSpending: [
        { name: "Food", value: 200, pct: 100, origIdx: 0 },
      ],
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    // The legend row should appear for the category
    expect(screen.getByText("Food")).toBeInTheDocument();
  });

  it("clicking legend row calls setChartFilter with the category name", () => {
    const setChartFilter = vi.fn();
    mockWith({
      donutData: [{ name: "Food", value: 200 }],
      sortedSpending: [
        { name: "Food", value: 200, pct: 100, origIdx: 0 },
      ],
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    // The legend buttons are the category rows — click the one with "Food"
    const btn = screen.getByRole("button", { name: /Food/i });
    fireEvent.click(btn);
    expect(setChartFilter).toHaveBeenCalledWith("Food");
  });

  it("shows active filter badge when chartFilter is set", () => {
    mockWith({
      donutData: [{ name: "Food", value: 200 }],
      sortedSpending: [{ name: "Food", value: 200, pct: 100, origIdx: 0 }],
      chartFilter: "Food",
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    expect(screen.getByText(/Filtering: Food/)).toBeInTheDocument();
  });

  it("clicking filter badge clears chartFilter (calls setChartFilter(null))", () => {
    const setChartFilter = vi.fn();
    mockWith({
      donutData: [{ name: "Food", value: 200 }],
      sortedSpending: [{ name: "Food", value: 200, pct: 100, origIdx: 0 }],
      chartFilter: "Food",
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "w1"))}</>);
    // The filter badge button text contains "Filtering: Food ×"
    const badge = screen.getByText(/Filtering: Food/);
    fireEvent.click(badge);
    expect(setChartFilter).toHaveBeenCalledWith(null);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// BudgetBarsWidget
// ══════════════════════════════════════════════════════════════════════════════

const MOCK_BUDGET = {
  id: 1,
  category_id: 10,
  category_name: "Groceries",
  amount: 500,
  spent: 300,
  remaining: 200,
  percent_used: 60,
  period_start: "2026-06-01",
  period_end: "2026-06-30",
};

describe("BudgetBarsWidget", () => {
  it("renders 'Budget Progress' heading", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    expect(screen.getByText("Budget Progress")).toBeInTheDocument();
  });

  it("renders empty state for current period when budgets is empty", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    expect(screen.getByText(/No budgets for this period/)).toBeInTheDocument();
  });

  it("renders past period empty state when isPastSelectedPeriod=true", () => {
    mockWith({ isPastSelectedPeriod: true });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    expect(screen.getByText(/No budgets were set for this period/)).toBeInTheDocument();
  });

  it("renders future period empty state when isFutureSelectedPeriod=true", () => {
    mockWith({ isFutureSelectedPeriod: true });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    expect(screen.getByText(/Future budgets live in Forecasts/)).toBeInTheDocument();
  });

  it("renders chart when budgets has items", () => {
    mockWith({
      budgets: [MOCK_BUDGET],
      dashBudgets: [MOCK_BUDGET],
      budgetChartData: [
        { name: "Groceries", spent: 300, remaining: 200, pct: 60 },
      ],
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    // The chart container should be present (mocked ResponsiveContainer)
    expect(screen.getByTestId("responsive-container")).toBeInTheDocument();
  });

  it("clicking spent bar calls setChartFilter with the category name", () => {
    const setChartFilter = vi.fn();
    mockWith({
      budgets: [MOCK_BUDGET],
      dashBudgets: [MOCK_BUDGET],
      budgetChartData: [{ name: "Groceries", spent: 300, remaining: 200, pct: 60 }],
      chartFilter: null,
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    fireEvent.click(screen.getByTestId("bar-spent"));
    expect(setChartFilter).toHaveBeenCalledWith("Groceries");
  });

  it("clicking spent bar when already active toggles filter to null", () => {
    const setChartFilter = vi.fn();
    mockWith({
      budgets: [MOCK_BUDGET],
      dashBudgets: [MOCK_BUDGET],
      budgetChartData: [{ name: "Groceries", spent: 300, remaining: 200, pct: 60 }],
      chartFilter: "Groceries",
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "w2"))}</>);
    fireEvent.click(screen.getByTestId("bar-spent"));
    expect(setChartFilter).toHaveBeenCalledWith(null);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// ForecastBarsWidget
// ══════════════════════════════════════════════════════════════════════════════

const MOCK_FORECAST = {
  id: 1,
  org_id: 1,
  period_start: "2026-06-01",
  total_planned_expense: 1000,
  total_planned_income: 2000,
  items: [],
};

const MOCK_FORECAST_EXPENSE_ITEM = {
  id: 1,
  plan_id: 1,
  category_id: 5,
  category_name: "Transport",
  parent_id: null,
  type: "expense" as const,
  planned_amount: "200.00",
  actual_amount: "150.00",
  variance: "50.00",
  source: "manual" as const,
};

describe("ForecastBarsWidget", () => {
  it("renders 'Forecast by Category' heading", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    expect(screen.getByText("Forecast by Category")).toBeInTheDocument();
  });

  it("renders empty state when forecast is null", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    expect(screen.getByText(/No forecast for this period/)).toBeInTheDocument();
  });

  it("renders empty state when forecastExpenseItems is empty even with forecast", () => {
    mockWith({
      forecast: MOCK_FORECAST,
      forecastExpenseItems: [],
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    expect(screen.getByText(/No forecast for this period/)).toBeInTheDocument();
  });

  it("renders chart when forecast has expense items", () => {
    mockWith({
      forecast: MOCK_FORECAST,
      forecastExpenseItems: [MOCK_FORECAST_EXPENSE_ITEM],
      forecastChartRows: [
        {
          categoryId: 5,
          name: "Transport",
          planned: 200,
          actual: 150,
        },
      ],
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    expect(screen.getByTestId("responsive-container")).toBeInTheDocument();
  });

  it("clicking planned bar calls setChartFilter with the category name", () => {
    const setChartFilter = vi.fn();
    mockWith({
      forecast: MOCK_FORECAST,
      forecastExpenseItems: [MOCK_FORECAST_EXPENSE_ITEM],
      forecastChartRows: [{ categoryId: 5, name: "Transport", planned: 200, actual: 150 }],
      chartFilter: null,
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    fireEvent.click(screen.getByTestId("bar-planned"));
    expect(setChartFilter).toHaveBeenCalledWith("Transport");
  });

  it("clicking planned bar when already active toggles filter to null", () => {
    const setChartFilter = vi.fn();
    mockWith({
      forecast: MOCK_FORECAST,
      forecastExpenseItems: [MOCK_FORECAST_EXPENSE_ITEM],
      forecastChartRows: [{ categoryId: 5, name: "Transport", planned: 200, actual: 150 }],
      chartFilter: "Transport",
      setChartFilter,
    });
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "w3"))}</>);
    fireEvent.click(screen.getByTestId("bar-planned"));
    expect(setChartFilter).toHaveBeenCalledWith(null);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// emptyDashboardWidget — 3 new types
// ══════════════════════════════════════════════════════════════════════════════

describe("emptyDashboardWidget — 3 new chart tile types", () => {
  it("dash_spending returns correct grid {x:0,y:8,w:4,h:5}", () => {
    const w = emptyDashboardWidget("dash_spending", "ws");
    expect(w.grid).toEqual({ x: 0, y: 8, w: 4, h: 5 });
  });

  it("dash_budget returns correct grid {x:4,y:8,w:4,h:5}", () => {
    const w = emptyDashboardWidget("dash_budget", "wb");
    expect(w.grid).toEqual({ x: 4, y: 8, w: 4, h: 5 });
  });

  it("dash_forecast_category returns correct grid {x:8,y:8,w:4,h:5}", () => {
    const w = emptyDashboardWidget("dash_forecast_category", "wf");
    expect(w.grid).toEqual({ x: 8, y: 8, w: 4, h: 5 });
  });

  const NEW_TYPES: DashboardWidgetType[] = [
    "dash_spending",
    "dash_budget",
    "dash_forecast_category",
  ];

  it.each(NEW_TYPES)("has a non-empty title for %s", (type) => {
    const w = emptyDashboardWidget(type, "t");
    expect(w.title.length).toBeGreaterThan(0);
  });

  it.each(NEW_TYPES)("config is {} for %s", (type) => {
    const w = emptyDashboardWidget(type, "t");
    expect(w.config).toEqual({});
  });

  it("each call returns an independent grid object (no aliasing)", () => {
    const a = emptyDashboardWidget("dash_spending", "a");
    const b = emptyDashboardWidget("dash_spending", "b");
    a.grid.x = 99;
    expect(b.grid.x).toBe(0);
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// renderDashboardWidget — 3 new arms
// ══════════════════════════════════════════════════════════════════════════════

describe("renderDashboardWidget — 3 new chart tile types", () => {
  it("dash_spending renders SpendingDonutWidget (check for 'Spending by Category')", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_spending", "ws"))}</>);
    expect(screen.getByText("Spending by Category")).toBeInTheDocument();
  });

  it("dash_budget renders BudgetBarsWidget (check for 'Budget Progress')", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_budget", "wb"))}</>);
    expect(screen.getByText("Budget Progress")).toBeInTheDocument();
  });

  it("dash_forecast_category renders ForecastBarsWidget (check for 'Forecast by Category')", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_forecast_category", "wf"))}</>);
    expect(screen.getByText("Forecast by Category")).toBeInTheDocument();
  });

  it("dash_recent_transactions renders RecentTransactionsWidget (check for 'Recent Transactions')", () => {
    render(<>{renderDashboardWidget(emptyDashboardWidget("dash_recent_transactions", "wr"))}</>);
    expect(screen.getByText("Recent Transactions")).toBeInTheDocument();
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// RecentTransactionsWidget (Phase 2c)
// ══════════════════════════════════════════════════════════════════════════════

const TX: Transaction = {
  id: 1,
  account_id: 1,
  account_name: "Checking",
  category_id: 10,
  category_name: "Groceries",
  description: "Supermarket",
  amount: 50,
  type: "expense",
  status: "settled",
  linked_transaction_id: null,
  recurring_id: null,
  date: "2026-06-10",
  settled_date: null,
  is_imported: false,
  is_manual_adjustment: false,
  tags: [],
} as unknown as Transaction;

function renderRecentTx() {
  return render(
    <>{renderDashboardWidget(emptyDashboardWidget("dash_recent_transactions", "wr"))}</>,
  );
}

describe("RecentTransactionsWidget", () => {
  it("renders the 'No transactions this period.' empty state when canAdd and no rows", () => {
    mockWith({ transactions: [], sortedVisibleTxs: [], canAdd: true });
    renderRecentTx();
    expect(screen.getByText("No transactions this period.")).toBeInTheDocument();
  });

  it("renders the setup-incomplete empty state when canAdd is false", () => {
    mockWith({ transactions: [], sortedVisibleTxs: [], canAdd: false });
    renderRecentTx();
    expect(
      screen.getByText("Create accounts and categories first."),
    ).toBeInTheDocument();
  });

  it("renders a transaction row and clicking the status pill calls onToggleTransactionStatus", () => {
    const onToggleTransactionStatus = vi.fn().mockResolvedValue(undefined);
    mockWith({
      transactions: [TX],
      sortedVisibleTxs: [TX],
      txMap: new Map([[TX.id, TX]]),
      onToggleTransactionStatus,
    });
    renderRecentTx();
    expect(screen.getByText("Supermarket")).toBeInTheDocument();
    const pill = screen.getByRole("button", { name: /Mark as pending/i });
    fireEvent.click(pill);
    expect(onToggleTransactionStatus).toHaveBeenCalledWith(TX);
  });

  it("clicking a sort header calls toggleDashSort with that field", () => {
    const toggleDashSort = vi.fn();
    mockWith({
      transactions: [TX],
      sortedVisibleTxs: [TX],
      txMap: new Map([[TX.id, TX]]),
      toggleDashSort,
    });
    renderRecentTx();
    fireEvent.click(
      screen.getByRole("button", { name: /Sort transactions by amount/i }),
    );
    expect(toggleDashSort).toHaveBeenCalledWith("amount");
  });

  it("shows pagination when txTotal exceeds the page size and no chartFilter", () => {
    mockWith({
      transactions: [TX],
      sortedVisibleTxs: [TX],
      txMap: new Map([[TX.id, TX]]),
      txTotal: 25,
      page: 0,
      pageSize: 10,
      chartFilter: null,
    });
    renderRecentTx();
    expect(
      screen.getByRole("button", { name: /Next page/i }),
    ).toBeInTheDocument();
  });

  it("hides pagination while a chartFilter is active", () => {
    mockWith({
      transactions: [TX],
      sortedVisibleTxs: [TX],
      txMap: new Map([[TX.id, TX]]),
      txTotal: 25,
      page: 0,
      pageSize: 10,
      chartFilter: "Groceries",
    });
    renderRecentTx();
    expect(
      screen.queryByRole("button", { name: /Next page/i }),
    ).not.toBeInTheDocument();
  });
});
