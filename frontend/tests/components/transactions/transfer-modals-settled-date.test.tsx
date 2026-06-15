import { render, screen } from "@testing-library/react";

import LinkAsTransferModal from "@/components/transactions/LinkAsTransferModal";
import MarkAsTransferModal from "@/components/transactions/MarkAsTransferModal";
import UnpairTransferModal from "@/components/transactions/UnpairTransferModal";
import { apiFetch } from "@/lib/api";
import type { Account, Category, Transaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

function makeLeg(over: Partial<Transaction> = {}): Transaction {
  return {
    id: 1,
    account_id: 10,
    account_name: "Checking",
    category_id: 100,
    category_name: "Other",
    description: "Buffer",
    amount: 500,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-31",
    settled_date: "2026-06-15",
    is_imported: false,
    is_manual_adjustment: false,
    tags: [],
    ...over,
  };
}

describe("Transfer modals — settled date display (Task 11)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("LinkAsTransferModal shows the settled date for each leg", () => {
    render(
      <LinkAsTransferModal
        expenseLeg={makeLeg({ id: 1, type: "expense", date: "2026-05-31", settled_date: "2026-06-15" })}
        incomeLeg={makeLeg({ id: 2, type: "income", account_name: "Savings", date: "2026-05-31", settled_date: null })}
        onLinked={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    // Original date visible on both legs.
    expect(screen.getAllByText(/2026-05-31/).length).toBeGreaterThan(0);
    // Settled date visible for the leg that has one.
    expect(screen.getByText(/settled 2026-06-15/i)).toBeInTheDocument();
    // Em-dash settled for the unsettled leg.
    expect(screen.getByText(/settled —/i)).toBeInTheDocument();
  });

  it("UnpairTransferModal shows the settled date for each leg", () => {
    render(
      <UnpairTransferModal
        expenseLeg={makeLeg({ id: 1, type: "expense", date: "2026-05-31", settled_date: "2026-06-15" })}
        incomeLeg={makeLeg({ id: 2, type: "income", account_name: "Savings", date: "2026-05-31", settled_date: "2026-06-15" })}
        categories={[] as Category[]}
        onUnpaired={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getAllByText(/settled 2026-06-15/i).length).toBeGreaterThan(0);
  });

  it("MarkAsTransferModal shows the settled date on the source line", () => {
    const accounts: Account[] = [
      {
        id: 10, name: "Checking", account_type_id: 1,
        account_type_name: "Checking", account_type_slug: "checking",
        balance: 0, currency: "EUR", is_active: true,
        close_day: null, is_default: true,
      },
      {
        id: 20, name: "Savings", account_type_id: 1,
        account_type_name: "Savings", account_type_slug: "savings",
        balance: 0, currency: "EUR", is_active: true,
        close_day: null, is_default: false,
      },
    ];
    render(
      <MarkAsTransferModal
        source={makeLeg({ id: 1, type: "expense", date: "2026-05-31", settled_date: "2026-06-15" })}
        accounts={accounts}
        onConverted={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getByText(/settled 2026-06-15/i)).toBeInTheDocument();
  });
});
