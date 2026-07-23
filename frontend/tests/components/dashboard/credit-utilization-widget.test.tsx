import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";
import CreditUtilizationWidget from "@/components/dashboard/widgets/CreditUtilizationWidget";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";
import type { Account } from "@/lib/types";

describe("CreditUtilizationBar", () => {
  it("labels a low-utilization card with just the percent (neutral band)", () => {
    render(<CreditUtilizationBar name="Visa" balance={-500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText("Visa")).toBeInTheDocument();
    expect(screen.getByText(/25%/)).toBeInTheDocument();
    expect(screen.queryByText(/Over limit/)).toBeNull();
  });
  it("labels a high-utilization card (>=75%) with High", () => {
    render(<CreditUtilizationBar name="Amex" balance={-1700} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/85%/)).toBeInTheDocument();
    expect(screen.getByText(/High/)).toBeInTheDocument();
  });
  it("labels an over-limit card with the overage in currency", () => {
    render(<CreditUtilizationBar name="Store" balance={-2500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/Over limit/)).toBeInTheDocument();
    expect(screen.getByText(/500/)).toBeInTheDocument();
  });
});

// ══════════════════════════════════════════════════════════════════════════════
// CreditUtilizationWidget
// ══════════════════════════════════════════════════════════════════════════════

// Mock strategy mirrors chart-widgets.test.tsx / dashboard-widget-registry.test.tsx:
// useDashboard is mocked at the module boundary and each test overrides only
// the fields it cares about via mockWith().

function makeAccount(overrides: Partial<Account>): Account {
  return {
    id: 1,
    name: "Card",
    account_type_id: 1,
    account_type_name: "Credit Card",
    account_type_slug: "credit_card",
    balance: 0,
    currency: "EUR",
    is_active: true,
    close_day: null,
    is_default: false,
    ...overrides,
  };
}

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
  spendingSort: {
    field: "amount",
    dir: "desc",
    setSort: vi.fn(),
    reset: vi.fn(),
    isDefault: true,
  },
  toggleSpendingSort: vi.fn(),
  forecastExpenseItems: [],
  forecastChartRows: [],
  chartFilter: null,
  setChartFilter: vi.fn(),
  transactions: [],
  txTotal: 0,
  page: 0,
  setPage: vi.fn(),
  pageSize: 10,
  setPageSize: vi.fn(),
  visibleTxs: [],
  sortedVisibleTxs: [],
  txMap: new Map(),
  dashSort: {
    field: "date",
    dir: "desc",
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

function mockWith(overrides: Partial<DashboardData>) {
  vi.mocked(useDashboard).mockReturnValueOnce({
    ...MOCK_DASHBOARD_DATA,
    ...overrides,
  });
}

describe("CreditUtilizationWidget", () => {
  it("renders one bar per credit card sorted by utilization desc", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, name: "Low Card", balance: -100, currency: "EUR", credit_limit: 1000 }),
        makeAccount({ id: 2, name: "High Card", balance: -900, currency: "EUR", credit_limit: 1000 }),
      ],
    });
    render(<CreditUtilizationWidget />);
    const names = screen.getAllByText(/Card$/).map((el) => el.textContent);
    expect(names).toEqual(["High Card", "Low Card"]);
  });

  it("excludes non-credit-card accounts", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, name: "Checking", account_type_slug: "checking", balance: 500 }),
        makeAccount({ id: 2, name: "Visa", balance: -100, credit_limit: 1000 }),
      ],
    });
    render(<CreditUtilizationWidget />);
    expect(screen.queryByText("Checking")).toBeNull();
    expect(screen.getByText("Visa")).toBeInTheDocument();
  });

  it("shows 'No limit set' for a CC with null/0 limit but a nonzero balance, excludes it from bars", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, name: "No Limit Card", balance: -50, credit_limit: null }),
      ],
    });
    render(<CreditUtilizationWidget />);
    expect(screen.getByText("No Limit Card")).toBeInTheDocument();
    expect(screen.getByText("No limit set")).toBeInTheDocument();
  });

  it("renders the empty state with an /accounts link when there are no credit cards", () => {
    mockWith({ activeAccounts: [] });
    render(<CreditUtilizationWidget />);
    expect(screen.getByText(/No credit cards yet/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Add one" });
    expect(link).toHaveAttribute("href", "/accounts");
  });

  it("shows a 'Next payment' chip sourced from the forecast's cc_payments, joined by account_id", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 7, name: "Amex", balance: -300, credit_limit: 1000, currency: "EUR" }),
      ],
      accountMonthEndForecast: {
        period_start: "2026-07-01",
        period_end: "2026-07-31",
        totals: [],
        accounts: [
          {
            account_id: 7,
            account_name: "Amex",
            currency: "EUR",
            is_default: false,
            account_type_slug: "credit_card",
            balance: "-300",
            pending_delta: "0",
            expected_month_end_balance: "-300",
            cc_payments: [{ amount: "300.00", date: "2026-07-15" }],
          },
        ],
      },
    });
    render(<CreditUtilizationWidget />);
    expect(screen.getByText(/Next payment/)).toBeInTheDocument();
    expect(screen.getByText(/300/)).toBeInTheDocument();
    expect(screen.getByText(/2026-07-15/)).toBeInTheDocument();
  });
});
