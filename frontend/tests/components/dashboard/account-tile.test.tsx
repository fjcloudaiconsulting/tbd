import { render, screen } from "@testing-library/react";

import AccountTilesCard, {
  AccountTileRow,
} from "@/components/dashboard/AccountTile";
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

describe("AccountTileRow — identity/status/navigation surface", () => {
  it("renders account name and account-type label", () => {
    render(<AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />);
    // Both the account name and the account-type label are "Checking",
    // so we expect two matches: name (medium-emphasis) + type (muted
    // subtext).
    expect(screen.getAllByText(/^Checking$/)).toHaveLength(2);
  });

  it("renders the currency code", () => {
    render(<AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />);
    expect(screen.getByText(/^EUR$/)).toBeInTheDocument();
  });

  it("shows the Primary badge on the default account, not on others", () => {
    const { rerender } = render(
      <AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />,
    );
    expect(screen.getByText(/^Primary$/i)).toBeInTheDocument();

    rerender(<AccountTileRow account={SECONDARY_SAVINGS} hasPending={false} />);
    expect(screen.queryByText(/^Primary$/i)).not.toBeInTheDocument();
  });

  it("shows a Pending badge only when hasPending is true", () => {
    const { rerender } = render(
      <AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />,
    );
    expect(screen.queryByText(/^Pending$/i)).not.toBeInTheDocument();

    rerender(<AccountTileRow account={PRIMARY_CHECKING} hasPending={true} />);
    expect(screen.getByText(/^Pending$/i)).toBeInTheDocument();
  });

  it("renders as a link to /accounts (click-through navigation)", () => {
    render(<AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />);
    const link = screen.getByTestId("account-tile");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "/accounts");
  });

  it("balance text is muted (forecast card is the numeric authority, tile is secondary)", () => {
    render(<AccountTileRow account={PRIMARY_CHECKING} hasPending={false} />);
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

describe("AccountTilesCard — unified card with internal divider rows", () => {
  it("wraps multiple account rows in a single card with divide-y rows (mirrors Forecast card idiom)", () => {
    render(
      <AccountTilesCard
        accounts={[PRIMARY_CHECKING, SECONDARY_SAVINGS]}
        pendingByAccount={{}}
      />,
    );
    const outerCard = screen.getByTestId("account-tiles-card");
    expect(outerCard).toBeInTheDocument();
    // Inner rows live inside the same card and use a divide-y
    // container (NOT a flex/grid stack of standalone card siblings).
    expect(outerCard.querySelector(".divide-y")).not.toBeNull();
    expect(screen.getAllByTestId("account-tile")).toHaveLength(2);
  });

  it("renders nothing when accounts is empty", () => {
    const { container } = render(
      <AccountTilesCard accounts={[]} pendingByAccount={{}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("forwards pending state per account to the rows", () => {
    render(
      <AccountTilesCard
        accounts={[PRIMARY_CHECKING, SECONDARY_SAVINGS]}
        pendingByAccount={{
          [PRIMARY_CHECKING.id]: -50,
          [SECONDARY_SAVINGS.id]: 0,
        }}
      />,
    );
    // Exactly one Pending badge — only PRIMARY_CHECKING has a non-zero
    // pending delta.
    expect(screen.getAllByText(/^Pending$/i)).toHaveLength(1);
  });
});
