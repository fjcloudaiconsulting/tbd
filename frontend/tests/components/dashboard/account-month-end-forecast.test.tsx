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
      screen.getByText(
        /Current balance plus everything still expected in this period\./,
      ),
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
      screen.getByText(
        /Current balance plus everything still expected in this period\./,
      ),
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
  it("renders a Change link deep-linking to that card's editor (/accounts?edit=<id>)", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: CC_WITH_PAYMENT })} />);
    const change = screen.getByRole("link", { name: /change/i });
    expect(change).toBeInTheDocument();
    // CC row account_id is 1 -> opens that account's inline editor.
    expect(change.getAttribute("href")).toBe("/accounts?edit=1");
  });
  it("renders no Change link when there are no cc_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />);
    expect(screen.queryByRole("link", { name: /change/i })).toBeNull();
  });
});

const LOAN_WITH_PAYMENT: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "-10000.00",
      pending_delta: "0.00",
      expected_month_end_balance: "-10000.00",
    },
  ],
  accounts: [
    {
      account_id: 2,
      account_name: "Car Loan",
      currency: "EUR",
      is_default: false,
      account_type_slug: "loan",
      balance: "-10000.00",
      pending_delta: "0.00",
      expected_month_end_balance: "-9768.00",
      loan_payments: [{ amount: "232.00", date: "2026-05-15" }],
    },
  ],
};

describe("AccountMonthEndForecast — loan projected payment", () => {
  it("renders a muted Payment line from loan_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: LOAN_WITH_PAYMENT })} />);
    const line = screen.getByText(/Payment.*€232\.00 on 2026-05-15/);
    expect(line).toBeInTheDocument();
    expect(line.className).toContain("text-text-muted");
    expect(line.className).toContain("text-[10px]");
  });

  it("deep-links Change to the loan's editor (/accounts?edit=<id>)", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: LOAN_WITH_PAYMENT })} />);
    const change = screen.getByRole("link", { name: /change/i });
    expect(change.getAttribute("href")).toBe("/accounts?edit=2");
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

// ═══════════════════════════════════════════════════════════════════════════
// TBD-198 — Low balance day warning
// ═══════════════════════════════════════════════════════════════════════════

// A fully populated fixture: two accounts, a CC payment line, a pending
// subtext. F9 runs on this SAME shape with `risk_days: []`, so it cannot pass
// merely because the widget fell into an empty state.
const POPULATED_WITH_RISK: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "500.00",
      pending_delta: "-600.00",
      expected_month_end_balance: "-100.00",
    },
  ],
  accounts: [
    {
      account_id: 1,
      account_name: "Checking",
      currency: "EUR",
      is_default: true,
      account_type_slug: "checking",
      balance: "500.00",
      pending_delta: "-600.00",
      expected_month_end_balance: "-100.00",
      daily_balances: [
        { date: "2026-05-12", balance: "-100.00" },
        { date: "2026-05-31", balance: "-100.00" },
      ],
      risk_days: [
        {
          from: "2026-05-12",
          through: "2026-05-31",
          lowest_balance: "-100.00",
          lowest_on: "2026-05-12",
        },
      ],
    },
    {
      account_id: 2,
      account_name: "Visa",
      currency: "EUR",
      is_default: false,
      account_type_slug: "credit_card",
      balance: "-500.00",
      pending_delta: "0.00",
      expected_month_end_balance: "0.00",
      cc_payments: [{ amount: "500.00", date: "2026-05-01" }],
      risk_days: [],
    },
  ],
};

function withoutRisk(): AccountMonthEndForecastResponse {
  return {
    ...POPULATED_WITH_RISK,
    accounts: POPULATED_WITH_RISK.accounts.map((a) => ({ ...a, risk_days: [] })),
  };
}

describe("AccountMonthEndForecast — low balance warning (TBD-198)", () => {
  it("F8: the warning is legible without colour — accessible name, not a class", () => {
    // NON-VACUOUS BY CONSTRUCTION: this asserts on the ACCESSIBLE NAME and on
    // `aria-hidden` on the icon, never on the class string. A class assertion
    // (`toContain("bg-danger-dim")`) passes for a colour-only badge, which is
    // a hard WCAG 2.2 AA fail under docs/design/DESIGN.md and docs/product/PRODUCT.md.
    render(
      <AccountMonthEndForecast {...defaults({ forecast: POPULATED_WITH_RISK })} />,
    );

    const badge = screen.getByTestId("low-balance-badge-1");
    // The text a screen reader announces, with the sr-only prefix.
    expect(badge).toHaveTextContent(/Warning:\s*Low balance/);

    // VISIBLE text, computed by SUBTRACTING the sr-only nodes rather than by
    // matching the badge's `textContent` — which already contains the sr-only
    // prefix, so a naive `getByText("Low balance")` on the badge matches even
    // when every visible word has been moved into the sr-only span. Measured:
    // that was the previous form of this assertion and it survived the mutant
    // below.
    //
    // Mutant killed: collapsing the chip to
    //   <span className="sr-only">Warning: Low balance</span><TriangleAlert/>
    // i.e. a badge that is an icon and a colour to a sighted user. `visible`
    // is then "" and this goes red; `toHaveTextContent` above stays green.
    const srOnly = badge.querySelector(".sr-only");
    expect(srOnly).not.toBeNull();
    const visible = Array.from(badge.childNodes)
      .filter(
        (n) =>
          !(n instanceof HTMLElement && n.classList.contains("sr-only")),
      )
      .map((n) => n.textContent ?? "")
      .join("")
      .trim();
    expect(visible).toBe("Low balance");

    // The icon carries NO meaning and must not be announced.
    //
    // ⚠ SCOPE, MEASURED: deleting the explicit `aria-hidden="true"` from
    // the JSX does NOT turn this red — lucide-react adds `aria-hidden="true"`
    // itself whenever an icon has no children and no a11y prop
    // (`lucide-react.js`, the `hasA11yProp` branch). The explicit prop is kept
    // because MarkerChip writes it and because a11y must not ride on a library
    // default, but this assertion's real job is pinning the RENDERED output:
    // it goes red the moment the icon stops being a hidden lucide glyph — an
    // inlined raw `<svg>`, or an icon given an `aria-label`. Do not read it as
    // a fence on the prop.
    const icon = badge.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");

    // The badge sits in a flex row beside a `truncate` account name and the
    // DEFAULT chip, inside a `minmax(0,2fr)` grid column, so it is squeezable.
    // ⚠ Honest scope: this is a CLASS assertion and jsdom has no layout
    // engine, so it pins the declaration, not the rendered geometry. It is
    // here because losing `shrink-0` is silent everywhere else.
    expect(badge.className).toContain("shrink-0");
  });

  it("F8b: the dated sub-line names the day and the trough", () => {
    render(
      <AccountMonthEndForecast {...defaults({ forecast: POPULATED_WITH_RISK })} />,
    );
    // `-€100.00`, NOT `€-100.00`: `{sign}{symbol}{magnitude}` is the
    // convention the pending sub-line four lines above it already uses
    // ("Includes -€600.00 pending"), and the first draft of this line put the
    // sign inside the amount.
    expect(
      screen.getByText(
        /Below zero 2026-05-12 to 2026-05-31, lowest -€100\.00 on 2026-05-12/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/€-100\.00/)).toBeNull();
  });

  it("F8c: one line per RUN in the DOM, and the single-day copy branch", () => {
    const twoRuns: AccountMonthEndForecastResponse = {
      ...POPULATED_WITH_RISK,
      accounts: [
        {
          ...POPULATED_WITH_RISK.accounts[0],
          risk_days: [
            {
              from: "2026-05-10",
              through: "2026-05-11",
              lowest_balance: "-300.00",
              lowest_on: "2026-05-11",
            },
            {
              from: "2026-05-20",
              through: "2026-05-20",
              lowest_balance: "-50.00",
              lowest_on: "2026-05-20",
            },
          ],
        },
        POPULATED_WITH_RISK.accounts[1],
      ],
    };
    // ⚠ WHAT THIS DOES NOT PROVE. It is NOT a fence on R2 ("runs, not days").
    // R2 is a BACKEND property — whether `risk_days` carries one entry per
    // contiguous interval or one per day — and the fence for it is
    // `test_f7_runs_not_days_two_separate_dips`. This component receives
    // whatever the backend sends. What IS pinned here is the rendering: the
    // `.map()` emits one <p> per array entry (not one for the first, not one
    // joined line for all), the badge is emitted once per ROW regardless of
    // how many runs it carries, and the `r.from === r.through` copy branch
    // renders the single-date form.
    render(<AccountMonthEndForecast {...defaults({ forecast: twoRuns })} />);
    expect(screen.getAllByTestId("low-balance-line-1")).toHaveLength(2);
    // A one-day run reads as a single date, not as a degenerate range.
    expect(
      screen.getByText(/Below zero on 2026-05-20 \(-€50\.00\)/),
    ).toBeInTheDocument();
    // Exactly one badge on the row, however many runs it carries.
    expect(screen.getAllByTestId("low-balance-badge-1")).toHaveLength(1);
  });

  it("F9: quiet by default — nothing renders when risk_days is empty", () => {
    // NON-VACUOUS: the fixture is the SAME fully populated one (two accounts,
    // a cc_payments line, a pending subtext), with only `risk_days` emptied.
    // Running this on a bare fixture would let it pass because the widget fell
    // into an empty state rather than because the warning is conditional.
    render(<AccountMonthEndForecast {...defaults({ forecast: withoutRisk() })} />);

    // Proof the card really did render its content.
    expect(screen.getByText(/Payment.*€500\.00 on 2026-05-01/)).toBeInTheDocument();
    expect(screen.getByText(/Includes -€600\.00 pending/)).toBeInTheDocument();

    expect(screen.queryByTestId("low-balance-badge-1")).toBeNull();
    expect(screen.queryByTestId("low-balance-badge-2")).toBeNull();
    expect(screen.queryByTestId("low-balance-line-1")).toBeNull();
    expect(screen.queryByText(/Below zero/)).toBeNull();
  });

  it("F9b: a row whose risk_days key is absent entirely renders no warning", () => {
    // The legacy wire shape (and every existing fixture in this file) has no
    // `risk_days` key at all. `?? []` must hold, not throw.
    render(
      <AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />,
    );
    expect(screen.queryByText(/Below zero/)).toBeNull();
    expect(screen.queryByTestId("low-balance-badge-1")).toBeNull();
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// TBD-198 review — the forecast must RECONCILE on screen
//
// `expected_month_end_balance` stopped being `balance + pending_delta` in this
// ticket: it is now the last point of a daily walk that also carries projected
// card / loan payments AND upcoming recurring occurrences. The card rendered a
// sub-line for the first two and NOTHING for the third, under a caption that
// still said "plus pending items" — three numbers on one row that do not add
// up, with no line naming the difference. docs/product/PRODUCT.md's line-item-visibility
// principle is the standard these two fences hold the row to.
// ═══════════════════════════════════════════════════════════════════════════

// balance 1000 - pending 200 - recurring 350 = 450. Deliberately a fixture
// where `balance + pending_delta` is 800 and therefore NOT the forecast: on
// POPULATED_WITH_RISK (500 - 600 = -100) the two are equal and neither fence
// below would discriminate anything.
const RECONCILES_ONLY_WITH_RECURRING: AccountMonthEndForecastResponse = {
  period_start: "2026-05-01",
  series_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [
    {
      currency: "EUR",
      balance: "1000.00",
      pending_delta: "-200.00",
      expected_month_end_balance: "450.00",
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
      pending_delta: "-200.00",
      expected_month_end_balance: "450.00",
      recurring_lines: [
        { amount: "-350.00", date: "2026-05-10" },
      ],
      risk_days: [],
    },
  ],
};

describe("AccountMonthEndForecast — the row reconciles (TBD-198 review)", () => {
  it("F11: the hero caption does not claim the forecast is balance + pending", () => {
    // Mutant killed: the caption this branch shipped with — "Current balance
    // plus pending items in this period." — which is a FALSE statement about
    // the number directly above it the moment any recurring occurrence or any
    // projected card/loan payment is in the window.
    render(
      <AccountMonthEndForecast
        {...defaults({ forecast: RECONCILES_ONLY_WITH_RECURRING })}
      />,
    );
    expect(
      screen.getByText(
        /Current balance plus everything still expected in this period\./,
      ),
    ).toBeInTheDocument();
    // The claim itself, not merely the old sentence: nothing under the hero
    // may name `pending` as the only addition.
    expect(screen.queryByText(/plus pending items/)).toBeNull();
  });

  it("F12: an un-materialised recurring occurrence gets its own dated sub-line", () => {
    // Mutant killed: no `recurring_lines` block in the row at all — which is
    // what this branch shipped. The row then renders `Balance 1000.00`,
    // `Includes -€200.00 pending` and `450.00`, and 350.00 of the difference
    // is nowhere on screen. Every other assertion in this file is green
    // against that mutant.
    render(
      <AccountMonthEndForecast
        {...defaults({ forecast: RECONCILES_ONLY_WITH_RECURRING })}
      />,
    );

    // The three numbers the user is asked to reconcile are all present...
    expect(screen.getByText(/^1,000\.00$/)).toBeInTheDocument();
    expect(screen.getByText(/Includes -€200\.00 pending/)).toBeInTheDocument();
    // Twice: the hero (Σ of one row) and the row itself. A single-account
    // fixture makes the two literally the same number, which is the whole
    // claim — the caption sits over the hero and the sub-lines under the row.
    expect(screen.getAllByText(/^450\.00$/)).toHaveLength(2);
    // ...and so is the line that closes the gap between them.
    expect(
      screen.getByText(/Recurring -€350\.00 on 2026-05-10/),
    ).toBeInTheDocument();
    expect(screen.getAllByTestId("recurring-line-1")).toHaveLength(1);
  });

  it("F12b: income-signed occurrences render '+', and multiple lines are one <p> each", () => {
    const twoLines: AccountMonthEndForecastResponse = {
      ...RECONCILES_ONLY_WITH_RECURRING,
      accounts: [
        {
          ...RECONCILES_ONLY_WITH_RECURRING.accounts[0],
          recurring_lines: [
            { amount: "-350.00", date: "2026-05-10" },
            { amount: "2000.00", date: "2026-05-25" },
          ],
        },
      ],
    };
    render(<AccountMonthEndForecast {...defaults({ forecast: twoLines })} />);
    expect(screen.getAllByTestId("recurring-line-1")).toHaveLength(2);
    // Mutant killed: rendering the magnitude (as cc_payments / loan_payments
    // do), which labels a 2,000.00 salary the same way as a 2,000.00 outflow.
    expect(
      screen.getByText(/Recurring \+€2,000\.00 on 2026-05-25/),
    ).toBeInTheDocument();
  });

  it("F13: quiet by default — no recurring line when the list is empty or absent", () => {
    // BOTH cases, on a fixture that is otherwise fully populated so a pass
    // cannot come from the card falling into an empty state.
    const emptyList: AccountMonthEndForecastResponse = {
      ...RECONCILES_ONLY_WITH_RECURRING,
      accounts: [
        { ...RECONCILES_ONLY_WITH_RECURRING.accounts[0], recurring_lines: [] },
      ],
    };
    const { unmount } = render(
      <AccountMonthEndForecast {...defaults({ forecast: emptyList })} />,
    );
    // Proof the card really rendered its content.
    expect(screen.getByText(/Includes -€200\.00 pending/)).toBeInTheDocument();
    expect(screen.queryByTestId("recurring-line-1")).toBeNull();
    expect(screen.queryByText(/Recurring /)).toBeNull();
    unmount();

    // The key absent entirely (the legacy wire shape, and POPULATED_WITH_RISK
    // above): `?? []` must hold, not throw.
    render(
      <AccountMonthEndForecast {...defaults({ forecast: POPULATED_WITH_RISK })} />,
    );
    expect(screen.getByText(/Payment.*€500\.00 on 2026-05-01/)).toBeInTheDocument();
    expect(screen.queryByTestId("recurring-line-1")).toBeNull();
    expect(screen.queryByText(/Recurring /)).toBeNull();
  });
});
