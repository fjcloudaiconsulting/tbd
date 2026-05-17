import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import TransferForm from "@/components/floating/TransferForm";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const CHECKING = {
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

const SAVINGS = {
  id: 2,
  name: "Savings",
  account_type_id: 2,
  account_type_name: "Savings",
  account_type_slug: "savings",
  balance: 5000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: false,
};

const TRANSFER_CAT = {
  id: 99,
  name: "Transfer",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "transfer",
  is_system: true,
  transaction_count: 0,
};

describe("TransferForm", () => {
  it("renders the empty state when fewer than two active accounts exist", () => {
    render(
      <TransferForm
        accounts={[CHECKING]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );
    expect(
      screen.getByText(/Transfers move money between two accounts/i),
    ).toBeInTheDocument();
  });

  it("renders the form when there are two active accounts", () => {
    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );
    expect(screen.getByLabelText("From account")).toBeInTheDocument();
    expect(screen.getByLabelText("To account")).toBeInTheDocument();
    expect(screen.getByLabelText("Amount")).toBeInTheDocument();
  });

  it("filters the destination account select to exclude the source", () => {
    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );
    // Source is Checking (default). Destination select must not list Checking.
    const toSelect = screen.getByLabelText("To account") as HTMLSelectElement;
    const optionNames = Array.from(toSelect.options).map((o) => o.textContent);
    expect(optionNames).toContain("Savings");
    expect(optionNames).not.toContain("Checking");
  });

  it("submits a valid transfer and calls onSaved on default Save", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText("To account"), {
      target: { value: String(SAVINGS.id) },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "200.00" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
    expect(onTransactionAdded).toHaveBeenCalledTimes(1);

    // Asserts the form posts to the L3.x transfer endpoint, not a new one.
    const call = apiFetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/transactions/transfer",
    );
    expect(call).toBeTruthy();
    const body = JSON.parse(call![1]!.body as string);
    expect(body.from_account_id).toBe(CHECKING.id);
    expect(body.to_account_id).toBe(SAVINGS.id);
    expect(body.amount).toBe("200.00");
    expect(body.status).toBe("settled");
    // category_id is omitted when no override picked. Defaults server-side.
    expect(body.category_id).toBeUndefined();
  });

  it("keeps the panel open and clears fields on Save and add new", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    fireEvent.change(screen.getByLabelText("To account"), {
      target: { value: String(SAVINGS.id) },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "75.00" },
    });

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /Save and add new/i }),
      );
    });

    await waitFor(() => {
      expect(onTransactionAdded).toHaveBeenCalledTimes(1);
    });
    // Default Save's onSaved must NOT fire under add-new intent.
    expect(onSaved).not.toHaveBeenCalled();
    // Amount cleared so the user can keep typing.
    expect((screen.getByLabelText("Amount") as HTMLInputElement).value).toBe(
      "",
    );
    // From account preserved (typical from-one-account, multiple-legs pattern).
    expect(
      (screen.getByLabelText("From account") as HTMLSelectElement).value,
    ).toBe(String(CHECKING.id));
  });

  it("clears the destination when the source is changed to the current destination (P1: prevents from===to submit)", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );

    // Source defaults to Checking (is_default). Pick destination = Savings.
    const toSelect = screen.getByLabelText("To account") as HTMLSelectElement;
    fireEvent.change(toSelect, { target: { value: String(SAVINGS.id) } });
    expect(toSelect.value).toBe(String(SAVINGS.id));

    // Now flip source to Savings. The controlled toAccountId must clear.
    const fromSelect = screen.getByLabelText(
      "From account",
    ) as HTMLSelectElement;
    fireEvent.change(fromSelect, { target: { value: String(SAVINGS.id) } });

    expect(fromSelect.value).toBe(String(SAVINGS.id));
    // Stale state guard: destination is no longer Savings (would have
    // produced from===to). Empty value means the form is submit-invalid
    // because the `required` attribute on the select blocks submission.
    expect(toSelect.value).toBe("");

    // Sanity: attempting Save with the cleared destination must NOT
    // post a transfer where from_account_id === to_account_id.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });
    const transferCalls = apiFetchMock.mock.calls.filter(
      ([url]) => url === "/api/v1/transactions/transfer",
    );
    expect(transferCalls).toHaveLength(0);
  });

  it("resets the submit-intent ref when native HTML5 validation rejects a Save and add new attempt (P2 intent-leak guard)", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    const onSaved = vi.fn();
    const onTransactionAdded = vi.fn();

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={onSaved}
        onTransactionAdded={onTransactionAdded}
      />,
    );

    // Click "Save and add new" with the form invalid (no destination,
    // no amount). requestSubmit() runs validation, fires `invalid` on
    // the first failed control, our capture-phase listener resets the
    // intent ref to "save".
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: /Save and add new/i }),
      );
    });
    // Confirm no network call happened (validation blocked submit).
    expect(onTransactionAdded).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();

    // Now fill the form validly and hit the normal Save button. If the
    // intent ref leaked we'd see add-new behavior (panel stays open,
    // form clears). Correct behavior: onSaved fires exactly once.
    fireEvent.change(screen.getByLabelText("To account"), {
      target: { value: String(SAVINGS.id) },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "50.00" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      expect(onSaved).toHaveBeenCalledTimes(1);
    });
    // Sanity: only one transfer POST happened.
    const transferCalls = apiFetchMock.mock.calls.filter(
      ([url]) => url === "/api/v1/transactions/transfer",
    );
    expect(transferCalls).toHaveLength(1);
  });

  it("labels the category field as 'Transfer category' (not 'Category (optional)')", () => {
    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );
    // Architect-locked copy 2026-05-16: the transfer form's category
    // field must read "Transfer category" so users understand it is
    // not a generic income/expense category. The matcher is exact to
    // catch accidental reverts to "Category (optional)".
    expect(screen.getByText("Transfer category")).toBeInTheDocument();
    expect(
      screen.queryByText(/Category \(optional\)/i),
    ).not.toBeInTheDocument();
  });

  it("renders the transfer-compatible helper copy under the category field", () => {
    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );
    // Helper text must point users at transfer-compatible categories
    // (type=both). The previous copy promised "Override to track in
    // budgets", which gave no hint that arbitrary expense categories
    // would be rejected by the backend.
    expect(
      screen.getByText(/Transfers use one category shared by both legs/i),
    ).toBeInTheDocument();
    // Architect feedback on PR #296: helper copy must NOT suggest
    // "Debt Repayment" because the seeded Debt Repayment category is
    // type=expense (in SYSTEM_CATEGORIES) and would be rejected by
    // the backend on a transfer. Replacement points at Transfer +
    // Credit Card Payment + "another transfer-compatible category you
    // create".
    expect(
      screen.getByText(
        /Pick Transfer, Credit Card Payment, or another transfer-compatible category you create/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Debt Repayment/i),
    ).not.toBeInTheDocument();
  });

  it("hides income- and expense-only categories from the transfer category picker", () => {
    // Mixed-type fixture: only the type=both row should be selectable
    // in the transfer-form context. The picker uses typeFilter=BOTH,
    // so income/expense masters and subs are hidden entirely.
    const EXPENSE_CAT = {
      id: 200,
      name: "Groceries",
      type: "expense" as const,
      parent_id: null,
      parent_name: null,
      description: null,
      slug: "groceries",
      is_system: false,
      transaction_count: 0,
    };
    const INCOME_CAT = {
      id: 300,
      name: "Salary",
      type: "income" as const,
      parent_id: null,
      parent_name: null,
      description: null,
      slug: "salary",
      is_system: false,
      transaction_count: 0,
    };
    const TRANSFER_BOTH = {
      ...TRANSFER_CAT,
      type: "both" as const,
    };

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[EXPENSE_CAT, INCOME_CAT, TRANSFER_BOTH]}
        onSaved={() => {}}
      />,
    );
    // Open the transfer category dropdown. The form has both a
    // <label htmlFor="fab-xfer-category">Transfer category</label> and
    // a HelpAnchor with aria-label "Help: Transfer category", so we
    // target the combobox by its explicit name (`Transfer category`)
    // via the `name` matcher rather than `getByLabelText` (which would
    // match both the label and the help icon).
    const combobox = screen.getByRole("combobox", {
      name: "Transfer category",
    });
    fireEvent.focus(combobox);
    const listbox = screen.getByRole("listbox");
    // The type=both option is visible.
    expect(within(listbox).getByText("Transfer")).toBeInTheDocument();
    // Income-only and expense-only options are NOT visible.
    expect(within(listbox).queryByText("Groceries")).not.toBeInTheDocument();
    expect(within(listbox).queryByText("Salary")).not.toBeInTheDocument();
  });

  it("omits category_id from the request when no override is picked (server applies the Transfer default)", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockResolvedValue({} as never);

    render(
      <TransferForm
        accounts={[CHECKING, SAVINGS]}
        categories={[TRANSFER_CAT]}
        onSaved={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText("To account"), {
      target: { value: String(SAVINGS.id) },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "10.00" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    const call = apiFetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/transactions/transfer",
    );
    const body = JSON.parse(call![1]!.body as string);
    // Server-side default is the "Transfer" category, so the client
    // intentionally omits the key when the user did not override it.
    expect(body.category_id).toBeUndefined();
  });
});
