import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import LinkAsTransferModal from "@/components/transactions/LinkAsTransferModal";
import { apiFetch } from "@/lib/api";
import type { Transaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const expenseLeg: Transaction = {
  id: 1,
  account_id: 10,
  account_name: "Checking",
  category_id: 100,
  category_name: "Other",
  description: "Buffer to savings",
  amount: 500,
  type: "expense",
  status: "settled",
  linked_transaction_id: null,
  recurring_id: null,
  date: "2026-04-29",
  settled_date: "2026-04-29",
  is_imported: false,
};

const incomeLeg: Transaction = {
  id: 2,
  account_id: 20,
  account_name: "Savings",
  category_id: 100,
  category_name: "Other",
  description: "Buffer received",
  amount: 500,
  type: "income",
  status: "settled",
  linked_transaction_id: null,
  recurring_id: null,
  date: "2026-04-29",
  settled_date: "2026-04-29",
  is_imported: false,
};

describe("LinkAsTransferModal", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders both legs and the recategorize toggle (default checked)", () => {
    render(
      <LinkAsTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        onLinked={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getByText(/Checking/)).toBeInTheDocument();
    expect(screen.getByText(/Savings/)).toBeInTheDocument();
    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).toBeChecked();
  });

  it("submit calls POST /api/v1/transactions/pair with correct body", async () => {
    apiFetchMock.mockResolvedValueOnce([] as never);
    const onLinked = vi.fn();

    render(
      <LinkAsTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        onLinked={onLinked}
        onCancel={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Link as transfer/i }));
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalled());
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/transactions/pair",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expense_id: 1,
          income_id: 2,
          recategorize: true,
        }),
      })
    );
    await waitFor(() => expect(onLinked).toHaveBeenCalled());
  });

  it("calls onCancel when Cancel button clicked", () => {
    const onCancel = vi.fn();
    render(
      <LinkAsTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        onLinked={vi.fn()}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("displays error message when API rejects", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("Pair failed: invariant"));

    render(
      <LinkAsTransferModal
        expenseLeg={expenseLeg}
        incomeLeg={incomeLeg}
        onLinked={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Link as transfer/i }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/Pair failed: invariant/)
    );
  });
});
