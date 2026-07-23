import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

import BalancesByTypeTile from "@/components/dashboard/widgets/BalancesByTypeTile";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";
import type { Account } from "@/lib/types";

function makeAccount(overrides: Partial<Account>): Account {
  return {
    id: 1,
    name: "Account",
    account_type_id: 1,
    account_type_name: "Checking",
    account_type_slug: "checking",
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

describe("BalancesByTypeTile", () => {
  it("renders one row per account type with a pluralized count", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 1, account_type_name: "Checking", account_type_slug: "checking", balance: 100 }),
        makeAccount({ id: 2, account_type_id: 1, account_type_name: "Checking", account_type_slug: "checking", balance: 200 }),
        makeAccount({ id: 3, account_type_id: 2, account_type_name: "Savings", account_type_slug: "savings", balance: 500 }),
      ],
    });
    render(<BalancesByTypeTile />);
    const rows = screen.getAllByTestId("balances-by-type-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Checking")).toBeInTheDocument();
    expect(within(rows[0]).getByText("2 accounts")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Savings")).toBeInTheDocument();
    expect(within(rows[1]).getByText("1 account")).toBeInTheDocument();
  });

  it("sums balances per type and coerces string wire values (no concatenation)", () => {
    mockWith({
      activeAccounts: [
        // balances arrive as strings on the wire despite the number TS type
        makeAccount({ id: 1, balance: "1000.50" as unknown as number }),
        makeAccount({ id: 2, balance: "3210.00" as unknown as number }),
      ],
    });
    render(<BalancesByTypeTile />);
    // 1000.50 + 3210.00 = 4210.50, not "1000.503210.00"
    expect(screen.getByText("4,210.50")).toBeInTheDocument();
  });

  it("shows one amount line per currency and never sums across currencies", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 2, account_type_name: "Savings", account_type_slug: "savings", balance: 12000, currency: "EUR" }),
        makeAccount({ id: 2, account_type_id: 2, account_type_name: "Savings", account_type_slug: "savings", balance: 1500, currency: "USD" }),
      ],
    });
    render(<BalancesByTypeTile />);
    const row = screen.getByTestId("balances-by-type-row");
    expect(within(row).getByText("12,000.00")).toBeInTheDocument();
    expect(within(row).getByText("EUR")).toBeInTheDocument();
    expect(within(row).getByText("1,500.00")).toBeInTheDocument();
    expect(within(row).getByText("USD")).toBeInTheDocument();
  });

  it("renders a liability subtotal with its stored negative sign and NO status color", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 4, account_type_name: "Credit card", account_type_slug: "credit_card", balance: -850 }),
      ],
    });
    const { container } = render(<BalancesByTypeTile />);
    expect(screen.getByText("-850.00")).toBeInTheDocument();
    // house rule: the sign carries the meaning; no danger/coral treatment
    expect(container.querySelector(".text-danger")).toBeNull();
  });

  it("includes accounts on custom (null-slug) account types", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 9, account_type_name: "Crypto", account_type_slug: null, balance: 4200 }),
      ],
    });
    render(<BalancesByTypeTile />);
    const row = screen.getByTestId("balances-by-type-row");
    expect(within(row).getByText("Crypto")).toBeInTheDocument();
    expect(within(row).getByText("4,200.00")).toBeInTheDocument();
  });

  it("orders types assets-first, liabilities-last, custom types after", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 4, account_type_name: "Credit card", account_type_slug: "credit_card", balance: -10 }),
        makeAccount({ id: 2, account_type_id: 9, account_type_name: "Crypto", account_type_slug: null, balance: 10 }),
        makeAccount({ id: 3, account_type_id: 1, account_type_name: "Checking", account_type_slug: "checking", balance: 10 }),
      ],
    });
    render(<BalancesByTypeTile />);
    const names = screen
      .getAllByTestId("balances-by-type-row")
      .map((r) => within(r).getByText(/Checking|Credit card|Crypto/).textContent);
    expect(names).toEqual(["Checking", "Credit card", "Crypto"]);
  });

  it("collapses 3+ currencies to the top 2 by magnitude plus a '+N more' note", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, balance: 50, currency: "GBP" }),
        makeAccount({ id: 2, balance: 5000, currency: "EUR" }),
        makeAccount({ id: 3, balance: 900, currency: "USD" }),
      ],
    });
    render(<BalancesByTypeTile />);
    const row = screen.getByTestId("balances-by-type-row");
    expect(within(row).getByText("5,000.00")).toBeInTheDocument(); // EUR (largest)
    expect(within(row).getByText("900.00")).toBeInTheDocument(); // USD (2nd)
    expect(within(row).queryByText("50.00")).toBeNull(); // GBP hidden
    expect(within(row).getByText("+1 more")).toBeInTheDocument();
  });

  it("gives each row an aria-label that spells the sign of a negative subtotal", () => {
    mockWith({
      activeAccounts: [
        makeAccount({ id: 1, account_type_id: 4, account_type_name: "Credit card", account_type_slug: "credit_card", balance: -850 }),
      ],
    });
    render(<BalancesByTypeTile />);
    expect(
      screen.getByRole("link", { name: "Credit card, 1 account, minus 850.00 EUR" }),
    ).toBeInTheDocument();
  });

  it("renders an empty state with an /accounts link when there are no accounts", () => {
    mockWith({ activeAccounts: [] });
    render(<BalancesByTypeTile />);
    expect(screen.getByText(/No accounts yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add one" })).toHaveAttribute("href", "/accounts");
  });
});
