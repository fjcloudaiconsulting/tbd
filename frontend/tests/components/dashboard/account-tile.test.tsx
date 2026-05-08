import { render, screen } from "@testing-library/react";

import AccountTile from "@/components/dashboard/AccountTile";
import type { Account } from "@/lib/types";

const PRIMARY_CHECKING: Account = {
  id: 1,
  name: "Checking",
  account_type_id: 10,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000 as unknown as number,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const SECONDARY_SAVINGS: Account = {
  id: 2,
  name: "Savings",
  account_type_id: 11,
  account_type_name: "Savings",
  account_type_slug: "savings",
  balance: 5000 as unknown as number,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: false,
};

describe("AccountTile — identity/status/navigation surface", () => {
  it("renders account name and account-type label", () => {
    render(<AccountTile account={PRIMARY_CHECKING} hasPending={false} />);
    // Both the account name and the account-type label are "Checking",
    // so we expect two matches: name (medium-emphasis) + type (muted
    // subtext).
    expect(screen.getAllByText(/^Checking$/)).toHaveLength(2);
  });

  it("renders the currency code", () => {
    render(<AccountTile account={PRIMARY_CHECKING} hasPending={false} />);
    expect(screen.getByText(/^EUR$/)).toBeInTheDocument();
  });

  it("shows the Primary badge on the default account, not on others", () => {
    const { rerender } = render(
      <AccountTile account={PRIMARY_CHECKING} hasPending={false} />,
    );
    expect(screen.getByText(/^Primary$/i)).toBeInTheDocument();

    rerender(<AccountTile account={SECONDARY_SAVINGS} hasPending={false} />);
    expect(screen.queryByText(/^Primary$/i)).not.toBeInTheDocument();
  });

  it("shows a Pending badge only when hasPending is true", () => {
    const { rerender } = render(
      <AccountTile account={PRIMARY_CHECKING} hasPending={false} />,
    );
    expect(screen.queryByText(/^Pending$/i)).not.toBeInTheDocument();

    rerender(<AccountTile account={PRIMARY_CHECKING} hasPending={true} />);
    expect(screen.getByText(/^Pending$/i)).toBeInTheDocument();
  });

  it("renders as a link to /accounts (click-through navigation)", () => {
    render(<AccountTile account={PRIMARY_CHECKING} hasPending={false} />);
    const link = screen.getByTestId("account-tile");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "/accounts");
  });

  it("balance text is muted (forecast card is the numeric authority, tile is secondary)", () => {
    render(<AccountTile account={PRIMARY_CHECKING} hasPending={false} />);
    // 1,000.00 appears, but as small muted secondary text — not the
    // primary visual anchor of the tile.
    const balance = screen.getByText(/1,000\.00/);
    expect(balance.className).toMatch(/text-text-muted/);
    // Crucially, the tile does NOT render the old large balance number
    // styled as the primary content (text-xl + tabular-nums + text-text-primary).
    expect(balance.className).not.toMatch(/text-xl/);
    expect(balance.className).not.toMatch(/font-semibold/);
  });
});
