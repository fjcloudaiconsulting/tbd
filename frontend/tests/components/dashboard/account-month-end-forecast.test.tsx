import { fireEvent, render, screen } from "@testing-library/react";

import AccountMonthEndForecast, {
  type AccountMonthEndForecastResponse,
} from "@/components/dashboard/AccountMonthEndForecast";

function defaults(
  overrides: Partial<Parameters<typeof AccountMonthEndForecast>[0]> = {},
) {
  return {
    forecast: null,
    isCurrentPeriod: true,
    hasAnyAccounts: true,
    onJumpToCurrent: vi.fn(),
    ...overrides,
  };
}

const TWO_ACCOUNTS_EUR: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "6000.00",
      pending_delta: "-150.00",
      expected_month_end_balance: "5850.00",
    },
  ],
  accounts: [
    {
      account_id: 1,
      account_name: "Checking",
      currency: "EUR",
      is_default: true,
      account_type_slug: "checking",
      balance: "1000.00",
      pending_delta: "-250.00",
      expected_month_end_balance: "750.00",
    },
    {
      account_id: 2,
      account_name: "Savings",
      currency: "EUR",
      is_default: false,
      account_type_slug: "savings",
      balance: "5000.00",
      pending_delta: "100.00",
      expected_month_end_balance: "5100.00",
    },
  ],
};

const TWO_CURRENCIES: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "1000.00",
      pending_delta: "0.00",
      expected_month_end_balance: "1000.00",
    },
    {
      currency: "USD",
      balance: "200.00",
      pending_delta: "-50.00",
      expected_month_end_balance: "150.00",
    },
  ],
  accounts: [
    {
      account_id: 1,
      account_name: "Checking EUR",
      currency: "EUR",
      is_default: true,
      account_type_slug: "checking",
      balance: "1000.00",
      pending_delta: "0.00",
      expected_month_end_balance: "1000.00",
    },
    {
      account_id: 2,
      account_name: "USD Cash",
      currency: "USD",
      is_default: false,
      account_type_slug: "cash",
      balance: "200.00",
      pending_delta: "-50.00",
      expected_month_end_balance: "150.00",
    },
  ],
};

describe("AccountMonthEndForecast — current period", () => {
  it("renders the eyebrow as the card's h2 (page outline preserved); no redundant 'Forecast' title", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    // Header consolidated: the explicit "Forecast" h2 title is gone,
    // but the page outline must stay consistent with the loading /
    // error / non-current-period branches that DO render an h2.
    // The "Expected month-end balance" eyebrow now carries the h2
    // role with the same eyebrow visual.
    const heading = screen.getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent(/^Expected month-end balance$/i);
    expect(heading.textContent).not.toMatch(/^Forecast$/);
    expect(
      screen.getByText(/Current balance plus pending items in this period\./),
    ).toBeInTheDocument();
  });

  it("renders the expected month-end balance per currency with a single descriptive line", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    // Top summary
    expect(screen.getByText(/expected month-end balance/i)).toBeInTheDocument();
    // EUR aggregate value
    expect(screen.getByText(/5,850\.00/)).toBeInTheDocument();
    // The single descriptive line under the hero replaces the old
    // duplicate "Includes pending items in this period." sentence.
    expect(
      screen.getByText(/Current balance plus pending items in this period\./),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/^Includes pending items in this period\.$/),
    ).not.toBeInTheDocument();
  });

  it("renders Account / Balance / End of month forecast columns", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    expect(screen.getByText(/^Account$/)).toBeInTheDocument();
    expect(screen.getByText(/^Balance$/)).toBeInTheDocument();
    expect(screen.getByText(/^End of month forecast$/)).toBeInTheDocument();
  });

  it("default account renders with DEFAULT marker and appears first", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    const checking = screen.getByText("Checking");
    const savings = screen.getByText("Savings");
    expect(checking.compareDocumentPosition(savings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/^DEFAULT$/)).toBeInTheDocument();
  });

  it("shows the pending subtext only on rows whose pending delta is non-zero", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    // Checking pending: -250
    expect(screen.getByText(/Includes -€250\.00 pending/)).toBeInTheDocument();
    // Savings pending: +100
    expect(screen.getByText(/Includes \+€100\.00 pending/)).toBeInTheDocument();
  });

  it("renders one expected-balance row per currency without combining unlike currencies", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_CURRENCIES })} />,
    );
    // EUR + USD totals listed separately. The 1,000.00 value appears
    // both as the total summary AND as the per-account row, so look up
    // by currency code and assert both currencies are present.
    // Each currency code appears in BOTH the total headline and the
    // per-account row, so use getAllByText for multi-match safety.
    expect(screen.getAllByText(/^EUR$/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/^USD$/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/1,000\.00/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/150\.00/).length).toBeGreaterThanOrEqual(1);
  });
});

describe("AccountMonthEndForecast — non-current periods", () => {
  it("past period renders the neutral state and does not show columns", () => {
    render(
      <AccountMonthEndForecast
        {...defaults({ forecast: TWO_ACCOUNTS_EUR, isCurrentPeriod: false })}
      />,
    );
    expect(
      screen.getByText(
        /Month-end balance forecast is only available for the current period\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^End of month forecast$/)).not.toBeInTheDocument();
  });

  it("future period renders a Today action when onJumpToCurrent is provided", () => {
    const onJump = vi.fn();
    render(
      <AccountMonthEndForecast
        {...defaults({
          forecast: TWO_ACCOUNTS_EUR,
          isCurrentPeriod: false,
          onJumpToCurrent: onJump,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /today/i }));
    expect(onJump).toHaveBeenCalledOnce();
  });
});

describe("AccountMonthEndForecast — empty states", () => {
  it("renders nothing when there are no accounts (page-level empty state owns this)", () => {
    const { container } = render(
      <AccountMonthEndForecast {...defaults({ forecast: null, hasAnyAccounts: false })} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when there are no accounts even on a non-current period", () => {
    // Empty org viewing a past/future period must NOT see the neutral
    // month-end card — the page-level empty state owns this surface.
    const { container } = render(
      <AccountMonthEndForecast
        {...defaults({
          forecast: null,
          hasAnyAccounts: false,
          isCurrentPeriod: false,
        })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("does not show a zero-pending subtext on rows whose pending delta is exactly 0", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_CURRENCIES })} />,
    );
    expect(screen.queryByText(/Includes \+?€0\.00 pending/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Includes \+?\$0\.00 pending/)).not.toBeInTheDocument();
  });
});

const CC_WITH_PAYMENT: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "-500.00",
      pending_delta: "0.00",
      expected_month_end_balance: "-500.00",
    },
  ],
  accounts: [
    {
      account_id: 1,
      account_name: "Visa",
      currency: "EUR",
      is_default: false,
      account_type_slug: "credit_card",
      balance: "-500.00",
      pending_delta: "0.00",
      expected_month_end_balance: "0.00",
      cc_payments: [{ amount: "500.00", date: "2026-05-01" }],
    },
  ],
};

describe("AccountMonthEndForecast — credit-card projected payment", () => {
  it("renders a muted Payment line from cc_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: CC_WITH_PAYMENT })} />);
    const line = screen.getByText(/Payment.*€500\.00 on 2026-05-01/);
    expect(line).toBeInTheDocument();
    expect(line.className).toContain("text-text-muted");
    expect(line.className).toContain("text-[10px]");
  });

  it("renders no payment line when cc_payments is absent or empty", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />);
    expect(screen.queryByText(/Payment.*on /)).toBeNull();
  });
});

describe("AccountMonthEndForecast — contextual Change link", () => {
  it("renders a Change link on the imminent payment line pointing at /accounts", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: CC_WITH_PAYMENT })} />);
    const change = screen.getByRole("link", { name: /change/i });
    expect(change).toBeInTheDocument();
    expect(change.getAttribute("href")).toBe("/accounts");
  });
  it("renders no Change link when there are no cc_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />);
    expect(screen.queryByRole("link", { name: /change/i })).toBeNull();
  });
});

describe("AccountMonthEndForecast — error state", () => {
  it("renders an explicit error message when hasError is true (not 'Loading…')", () => {
    render(
      <AccountMonthEndForecast
        {...defaults({ forecast: null, hasError: true })}
      />,
    );
    expect(
      screen.getByText(/Couldn't load account forecast\. Try again later\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Loading…/)).not.toBeInTheDocument();
  });

  it("error state still renders nothing when there are no accounts", () => {
    const { container } = render(
      <AccountMonthEndForecast
        {...defaults({
          forecast: null,
          hasAnyAccounts: false,
          hasError: true,
        })}
      />,
    );
    expect(container.firstChild).toBeNull();
  });
});
