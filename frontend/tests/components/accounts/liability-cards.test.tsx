import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import LiabilityCards, { resolvePaymentSource } from "@/components/accounts/LiabilityCards";
import { formatMonthYear } from "@/lib/format";
import type { Account } from "@/lib/types";

// balance / opening_balance are typed `number` but arrive as Pydantic-Decimal
// strings at runtime (the known type-lie), so fixtures build a loose object
// and cast, matching the other accounts-page test fixtures.
function acct(over: Record<string, unknown>): Account {
  return {
    id: 1,
    name: "A",
    account_type_id: 1,
    account_type_name: "",
    account_type_slug: "checking",
    balance: "0.00",
    currency: "EUR",
    is_active: true,
    is_default: false,
    opening_balance: "0.00",
    ...over,
  } as unknown as Account;
}

describe("formatMonthYear", () => {
  test("formats ISO date as Mon YYYY", () => {
    expect(formatMonthYear("2031-03-15")).toBe("Mar 2031");
    expect(formatMonthYear("2054-06-01")).toBe("Jun 2054");
  });
  test("returns empty for null/undefined and passes through non-ISO", () => {
    expect(formatMonthYear(null)).toBe("");
    expect(formatMonthYear(undefined)).toBe("");
    expect(formatMonthYear("not-a-date")).toBe("not-a-date");
  });
});

describe("resolvePaymentSource", () => {
  const accounts = [
    acct({ id: 10, name: "Primary", is_active: true }),
    acct({ id: 11, name: "Old", is_active: false }),
  ];
  test("resolves an active source", () => {
    expect(resolvePaymentSource(accounts, 10)).toEqual({ name: "Primary", isActive: true });
  });
  test("flags an inactive source", () => {
    expect(resolvePaymentSource(accounts, 11)).toEqual({ name: "Old", isActive: false });
  });
  test("null id -> null; missing id -> unknown", () => {
    expect(resolvePaymentSource(accounts, null)).toBeNull();
    expect(resolvePaymentSource(accounts, 999)).toEqual({ name: "unknown", isActive: true });
  });
});

describe("LiabilityCards", () => {
  test("renders nothing when there are no liabilities", () => {
    const { container } = render(
      <LiabilityCards accounts={[acct({ id: 1, account_type_slug: "checking" })]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders a card only for liabilities, ignoring asset accounts", () => {
    render(
      <LiabilityCards
        accounts={[
          acct({ id: 1, account_type_slug: "checking", name: "Chk" }),
          acct({
            id: 2,
            account_type_slug: "credit_card",
            name: "Visa",
            balance: "-100.00",
            credit_limit: "1000.00",
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("cc-card-2")).toBeInTheDocument();
    // no loan in the fixture, and the checking account gets no card
    expect(screen.queryByTestId(/^loan-card-/)).toBeNull();
    expect(screen.queryByTestId("cc-card-1")).toBeNull();
  });

  test("orders credit cards by utilization desc and loans by balance magnitude desc", () => {
    render(
      <LiabilityCards
        accounts={[
          acct({ id: 1, account_type_slug: "credit_card", name: "Low", balance: "-100.00", credit_limit: "1000.00" }),
          acct({ id: 2, account_type_slug: "credit_card", name: "High", balance: "-900.00", credit_limit: "1000.00" }),
          acct({ id: 3, account_type_slug: "loan", name: "Small", balance: "-5000.00" }),
          acct({ id: 4, account_type_slug: "loan", name: "Big", balance: "-250000.00" }),
        ]}
      />,
    );
    const cards = screen.getAllByTestId(/^(cc|loan)-card-/);
    const ids = cards.map((c) => c.getAttribute("data-testid"));
    // CCs first (utilization desc: id 2 then 1), then loans (balance desc: 4 then 3).
    expect(ids).toEqual(["cc-card-2", "cc-card-1", "loan-card-4", "loan-card-3"]);
  });

  test("a loan with no computed metrics still renders a teachable card", () => {
    render(
      <LiabilityCards
        accounts={[acct({ id: 7, account_type_slug: "loan", name: "New loan", loan: null })]}
      />,
    );
    const card = screen.getByTestId("loan-card-7");
    expect(within(card).getByText(/Finish setting up this loan/)).toBeInTheDocument();
  });
});
