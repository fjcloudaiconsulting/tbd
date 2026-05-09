import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionForm from "@/components/floating/TransactionForm";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 10,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

describe("TransactionForm", () => {
  it("renders the empty state when there are no accounts or categories", () => {
    render(
      <TransactionForm
        accounts={[]}
        categories={[]}
        onSaved={() => {}}
      />,
    );
    expect(screen.getByText(/Create at least one account/i)).toBeInTheDocument();
  });

  it("submits a valid transaction and calls onSaved (default Save closes the panel)", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        defaultCategoryId={CAT.id}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Groceries Aldi" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "12.34" },
    });
    // Account defaults from the is_default fixture; category from prop.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
    expect(onTransactionAdded).toHaveBeenCalledTimes(1);
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    const [path, options] = apiFetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/transactions");
    expect(options?.method).toBe("POST");
    const body = JSON.parse(String(options?.body));
    expect(body.description).toBe("Groceries Aldi");
    expect(body.amount).toBe("12.34");
    expect(body.account_id).toBe(ACCT.id);
    expect(body.category_id).toBe(CAT.id);
    expect(body.type).toBe("expense");
    expect(body.status).toBe("settled");
  });

  it("Save and add new keeps the panel open and clears description and amount", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();

    render(
      <TransactionForm
        accounts={[ACCT]}
        categories={[CAT]}
        onSaved={onSaved}
      />,
    );

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "First" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "9.99" },
    });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /save and add new/i }),
      );
    });

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledTimes(1);
    });
    // Panel must stay open: onSaved must NOT have fired.
    expect(onSaved).not.toHaveBeenCalled();
    // Form must be cleared.
    const desc = screen.getByLabelText("Description") as HTMLInputElement;
    const amount = screen.getByLabelText("Amount") as HTMLInputElement;
    expect(desc.value).toBe("");
    expect(amount.value).toBe("");
  });

  it("flips status to pending when a credit-card account is selected", () => {
    const CREDIT = {
      ...ACCT,
      id: 2,
      name: "Visa",
      account_type_slug: "credit_card",
      is_default: false,
    };
    render(
      <TransactionForm
        accounts={[ACCT, CREDIT]}
        categories={[CAT]}
        onSaved={() => {}}
      />,
    );
    const status = screen.getByLabelText("Status") as HTMLSelectElement;
    expect(status.value).toBe("settled");
    fireEvent.change(screen.getByLabelText("Account"), {
      target: { value: String(CREDIT.id) },
    });
    expect(status.value).toBe("pending");
  });

  it("respects defaultAccountId when provided", () => {
    const SAVINGS = { ...ACCT, id: 99, name: "Savings", is_default: false };
    render(
      <TransactionForm
        accounts={[ACCT, SAVINGS]}
        categories={[CAT]}
        defaultAccountId={SAVINGS.id}
        onSaved={() => {}}
      />,
    );
    const account = screen.getByLabelText("Account") as HTMLSelectElement;
    expect(account.value).toBe(String(SAVINGS.id));
  });
});
