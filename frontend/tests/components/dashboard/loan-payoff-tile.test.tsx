import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

import LoanPayoffTile from "@/components/dashboard/widgets/LoanPayoffTile";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";
import type { Account, LoanMetrics } from "@/lib/types";

function loanMetrics(status: LoanMetrics["status"], over: Partial<LoanMetrics> = {}): LoanMetrics {
  return {
    expected_monthly_payment: "200.00",
    maturation_date: "2030-01-01",
    total_interest: "5000.00",
    projected_payoff_date: status === "interest_only" ? null : "2030-01-01",
    projected_payoff_months: status === "interest_only" ? null : 60,
    status,
    ...over,
  };
}

function makeLoan(overrides: Partial<Account>): Account {
  return {
    id: 1,
    name: "Loan",
    account_type_id: 9,
    account_type_name: "Loan",
    account_type_slug: "loan",
    balance: -10000,
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
  monthFrom: "2026-06-01",
  monthTo: "2026-06-30",
  jumpToCurrentPeriod: vi.fn(),
  budgets: [],
  dashBudgets: [],
  budgetChartData: [],
  donutData: [],
  totalSpend: 0,
  sortedSpending: [],
  spendingSort: { field: "amount", dir: "desc", setSort: vi.fn(), reset: vi.fn(), isDefault: true },
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
  dashSort: { field: "date", dir: "desc", setSort: vi.fn(), reset: vi.fn(), isDefault: true },
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
  return { ...actual, useDashboard: vi.fn(() => MOCK_DASHBOARD_DATA) };
});

function mockWith(overrides: Partial<DashboardData>) {
  vi.mocked(useDashboard).mockReturnValueOnce({ ...MOCK_DASHBOARD_DATA, ...overrides });
}

/** Build an accountMonthEndForecast with loan_payments for the given accounts. */
function forecastWith(rows: Array<{ id: number; currency: string; payment?: { amount: string; date: string } }>) {
  return {
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    totals: [],
    accounts: rows.map((r) => ({
      account_id: r.id,
      account_name: `A${r.id}`,
      currency: r.currency,
      is_default: false,
      account_type_slug: "loan",
      balance: "-10000",
      pending_delta: "0",
      expected_month_end_balance: "-10000",
      loan_payments: r.payment ? [r.payment] : [],
    })),
  } as DashboardData["accountMonthEndForecast"];
}

describe("LoanPayoffTile", () => {
  it("renders the empty state with an /accounts link when there are no loans", () => {
    mockWith({ activeAccounts: [] });
    render(<LoanPayoffTile />);
    expect(screen.getByText(/No loans yet/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add one" })).toHaveAttribute("href", "/accounts");
    expect(screen.queryByTestId("loan-payoff-row")).toBeNull();
  });

  it("excludes non-loan accounts", () => {
    mockWith({
      activeAccounts: [
        makeLoan({ id: 1, name: "Mortgage", loan: loanMetrics("on_track") }),
        makeLoan({ id: 2, name: "Checking", account_type_slug: "checking", loan: null }),
      ],
    });
    render(<LoanPayoffTile />);
    expect(screen.getByText("Mortgage")).toBeInTheDocument();
    expect(screen.queryByText("Checking")).toBeNull();
    expect(screen.getAllByTestId("loan-payoff-row")).toHaveLength(1);
  });

  it.each([
    ["on_track", "On track"],
    ["interest_only", "Interest only"],
    ["paid_off", "Paid off"],
  ] as const)("labels a %s loan as '%s'", (status, label) => {
    mockWith({ activeAccounts: [makeLoan({ id: 1, name: "L", loan: loanMetrics(status) })] });
    render(<LoanPayoffTile />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("labels a loan with null metrics as 'Needs setup' and shows no next-payment line", () => {
    mockWith({ activeAccounts: [makeLoan({ id: 1, name: "New loan", loan: null })] });
    render(<LoanPayoffTile />);
    expect(screen.getByText("Needs setup")).toBeInTheDocument();
    expect(screen.queryByText(/Next payment/)).toBeNull();
  });

  it("sorts attention-first: interest_only, setup, on_track, paid_off", () => {
    mockWith({
      activeAccounts: [
        makeLoan({ id: 1, name: "L_paid", loan: loanMetrics("paid_off") }),
        makeLoan({ id: 2, name: "L_ontrack", loan: loanMetrics("on_track") }),
        makeLoan({ id: 3, name: "L_setup", loan: null }),
        makeLoan({ id: 4, name: "L_int", loan: loanMetrics("interest_only") }),
      ],
    });
    render(<LoanPayoffTile />);
    const names = screen
      .getAllByTestId("loan-payoff-row")
      .map((row) => within(row).getByText(/^L_/).textContent);
    expect(names).toEqual(["L_int", "L_setup", "L_ontrack", "L_paid"]);
  });

  it("shows a 'Next payment' line sourced from loan_payments, joined by account_id, worded 'on {date}'", () => {
    mockWith({
      activeAccounts: [makeLoan({ id: 7, name: "Car loan", currency: "EUR", loan: loanMetrics("on_track") })],
      accountMonthEndForecast: forecastWith([{ id: 7, currency: "EUR", payment: { amount: "250.00", date: "2026-07-20" } }]),
    });
    render(<LoanPayoffTile />);
    expect(screen.getByText(/Next payment 250\.00 EUR on 2026-07-20/)).toBeInTheDocument();
  });

  it("omits the next-payment line when the forecast has no loan_payment for the account", () => {
    mockWith({
      activeAccounts: [makeLoan({ id: 7, name: "Car loan", loan: loanMetrics("on_track") })],
      accountMonthEndForecast: forecastWith([{ id: 7, currency: "EUR" }]),
    });
    render(<LoanPayoffTile />);
    expect(screen.queryByText(/Next payment/)).toBeNull();
  });

  it("degrades gracefully when accountMonthEndForecast is null (status still renders, no next-payment)", () => {
    mockWith({
      activeAccounts: [makeLoan({ id: 7, name: "Car loan", loan: loanMetrics("on_track") })],
      accountMonthEndForecast: null,
    });
    render(<LoanPayoffTile />);
    expect(screen.getByText("On track")).toBeInTheDocument();
    expect(screen.queryByText(/Next payment/)).toBeNull();
  });

  it("renders 'On track' even when projected_payoff_date is null (no crash)", () => {
    mockWith({
      activeAccounts: [
        makeLoan({ id: 1, name: "L", loan: loanMetrics("on_track", { projected_payoff_date: null }) }),
      ],
    });
    render(<LoanPayoffTile />);
    expect(screen.getByText("On track")).toBeInTheDocument();
  });

  it("keeps currencies separate for multi-currency loans and never sums them", () => {
    mockWith({
      activeAccounts: [
        makeLoan({ id: 1, name: "EUR loan", currency: "EUR", loan: loanMetrics("on_track") }),
        makeLoan({ id: 2, name: "USD loan", currency: "USD", loan: loanMetrics("on_track") }),
      ],
      accountMonthEndForecast: forecastWith([
        { id: 1, currency: "EUR", payment: { amount: "100.00", date: "2026-07-10" } },
        { id: 2, currency: "USD", payment: { amount: "200.00", date: "2026-07-11" } },
      ]),
    });
    render(<LoanPayoffTile />);
    expect(screen.getByText(/100\.00 EUR on 2026-07-10/)).toBeInTheDocument();
    expect(screen.getByText(/200\.00 USD on 2026-07-11/)).toBeInTheDocument();
    // No aggregate/summed figure (e.g. 300) anywhere.
    expect(screen.queryByText(/300/)).toBeNull();
  });

  it("is a glanceable summary: no balance and no detail-card metrics (rate/term/matures/interest)", () => {
    mockWith({
      activeAccounts: [
        makeLoan({
          id: 1,
          name: "Mortgage",
          balance: -123456,
          interest_rate_apr: 3.5,
          term_months: 240,
          loan: loanMetrics("on_track"),
        }),
      ],
    });
    render(<LoanPayoffTile />);
    // Guardrail: the tile must not restate balance or transplant the detail card.
    expect(screen.queryByText(/123,?456/)).toBeNull();
    expect(screen.queryByText(/Rate/i)).toBeNull();
    expect(screen.queryByText(/Term/i)).toBeNull();
    expect(screen.queryByText(/Matures/i)).toBeNull();
    expect(screen.queryByText(/Interest over term/i)).toBeNull();
  });
});
