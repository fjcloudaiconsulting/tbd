/**
 * TBD-268 fence F9 — RecentTransactionsWidget (custom dashboard canvas).
 *
 * The widget is a verbatim port of the legacy dashboard's recent-transactions
 * tile and carried the same client-side "hide the higher-id leg" rule (via the
 * provider's visibleTxs + txMap). With the collapse moved server-side, the
 * widget must render every row the provider hands it, resolve the partner
 * account name from `linked_account_name` rather than a page-local map, and
 * key its empty state off the RENDERED list.
 *
 * useDashboard is mocked at the module boundary so the widget renders without
 * a real provider — the same strategy as dashboard-widget-registry.test.tsx.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import RecentTransactionsWidget from "@/components/dashboard/widgets/RecentTransactionsWidget";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";
import type { Transaction } from "@/lib/types";

function tx(over: Partial<Transaction> = {}): Transaction {
  return {
    id: 1,
    account_id: 10,
    account_name: "Checking",
    category_id: 1,
    category_name: "Groceries",
    description: "Row",
    amount: 12,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-10",
    settled_date: "2026-05-11",
    is_imported: false,
    is_manual_adjustment: false,
    tags: [],
    ...over,
  } as Transaction;
}

// A collapsed page of 10: 8 plain rows plus 2 transfers whose surviving leg is
// the INCOME one (higher id, partner absent from the page). The legacy client
// hide removed exactly those two.
function collapsedPage(): Transaction[] {
  const rows: Transaction[] = [];
  for (let i = 1; i <= 8; i++) rows.push(tx({ id: i, description: `Row ${i}` }));
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

const BASE: DashboardData = {
  accounts: [],
  activeAccounts: [],
  pendingByAccount: {},
  forecast: null,
  forecastProjection: null,
  projectionFailed: false,
  projectionLoading: false,
  onRetryProjection: vi.fn(),
  rollupFailed: false,
  rollupLoading: false,
  onRetryRollup: vi.fn(),
  accountMonthEndForecast: null,
  accountMonthEndForecastError: false,
  periods: [],
  periodIdx: 0,
  setPeriodIdx: vi.fn(),
  selectedPeriod: null,
  isCurrentSelectedPeriod: true,
  isPastSelectedPeriod: false,
  isFutureSelectedPeriod: false,
  monthFrom: "2026-05-01",
  monthTo: "2026-05-31",
  jumpToCurrentPeriod: vi.fn(),
  budgets: [],
  dashBudgets: [],
  budgetChartData: [],
  donutData: [],
  totalSpend: 0,
  sortedSpending: [],
  spendingSort: {
    field: "amount", dir: "desc", setSort: vi.fn(), reset: vi.fn(), isDefault: true,
  },
  toggleSpendingSort: vi.fn(),
  forecastExpenseItems: [],
  forecastChartRows: [],
  chartFilter: null,
  chartFilterName: null,
  setChartFilter: vi.fn(),
  transactions: [],
  txTotal: 0,
  page: 0,
  setPage: vi.fn(),
  pageSize: 10,
  setPageSize: vi.fn(),
  sortedVisibleTxs: [],
  dashSort: {
    field: "date", dir: "desc", setSort: vi.fn(), reset: vi.fn(), isDefault: true,
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
  return { ...actual, useDashboard: vi.fn() };
});

function mount(over: Partial<DashboardData>) {
  vi.mocked(useDashboard).mockReturnValue({ ...BASE, ...over });
  render(<RecentTransactionsWidget />);
}

function rowIds(): number[] {
  return screen
    .getAllByTestId(/^dash-settled-\d+$/)
    .map((el) => Number(el.getAttribute("data-testid")!.replace("dash-settled-", "")));
}

describe("RecentTransactionsWidget — transfer collapse (TBD-268 F9)", () => {
  beforeEach(() => {
    vi.mocked(useDashboard).mockReset();
  });

  it("F9: renders every row the provider returns, transfers included", () => {
    const rows = collapsedPage();
    mount({ transactions: rows, sortedVisibleTxs: rows, txTotal: rows.length });

    expect(rowIds()).toHaveLength(10);
    // Kills the legacy hide surviving anywhere in the widget/provider chain.
    expect(rowIds()).toContain(101);
    expect(rowIds()).toContain(103);
  });

  it("F9b: resolves the partner account name off the row, not a page-local map", () => {
    // The partner (id 100) is deliberately ABSENT from every array here, so a
    // txMap lookup could not possibly resolve it. Direction comes from `type`,
    // so a surviving INCOME leg still reads source -> destination.
    const rows = collapsedPage();
    mount({ transactions: rows, sortedVisibleTxs: rows, txTotal: rows.length });

    expect(screen.getAllByText(/Checking\s*→\s*Savings/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Savings\s*→\s*Checking/)).toBeNull();
  });

  it("F9c: an empty rendered list shows the empty state, not a blank card", () => {
    // Keyed off sortedVisibleTxs, not `transactions`. TBD-221 made the two the
    // same array in the provider, but the WIDGET must not start reading the raw
    // page: it is handed both, and only one of them is the rendered list.
    mount({
      transactions: [tx({ id: 1 })],
      sortedVisibleTxs: [],
      txTotal: 1,
      chartFilter: 5,
      chartFilterName: "Groceries",
    });

    expect(screen.getByText(/No transactions this period/)).toBeInTheDocument();
  });
});
