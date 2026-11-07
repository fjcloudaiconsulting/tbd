import { fireEvent, render, screen, within } from "@testing-library/react";

import BatchEditModal from "@/components/transactions/BatchEditModal";
import type { Account, Category } from "@/lib/types";

// TagChipInput hits apiFetch for autocomplete suggestions on type. Mock it so
// the component renders without a network call in the test harness.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(async () => ({ suggestions: [] })) };
});

const CATEGORIES: Category[] = [
  {
    id: 11,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 12,
    name: "Salary",
    type: "income",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "salary",
    is_system: false,
    transaction_count: 0,
  },
];

const ACCOUNTS: Account[] = [
  {
    id: 100,
    name: "Checking",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "EUR",
    is_active: true,
    close_day: null,
    is_default: true,
  },
  {
    id: 101,
    name: "Old Savings",
    account_type_id: 1,
    account_type_name: "Bank",
    account_type_slug: "bank",
    balance: 0,
    currency: "EUR",
    is_active: false,
    close_day: null,
    is_default: false,
  },
];

function renderModal(over: Partial<React.ComponentProps<typeof BatchEditModal>> = {}) {
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  render(
    <BatchEditModal
      open
      count={3}
      categories={CATEGORIES}
      accounts={ACCOUNTS}
      submitting={false}
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...over}
    />,
  );
  return { onSubmit, onCancel };
}

describe("BatchEditModal", () => {
  it("renders nothing when open is false", () => {
    const { container } = render(
      <BatchEditModal
        open={false}
        count={3}
        categories={CATEGORIES}
        accounts={ACCOUNTS}
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the selected count, the four controls, and the transfer hint", () => {
    renderModal({ count: 5 });

    // Count surfaces in the dialog (title).
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/5/)).toBeTruthy();

    // Category picker (CategorySelect renders a combobox).
    expect(screen.getByRole("combobox", { name: /category/i })).toBeTruthy();

    // Status select with a "No change" default option.
    const status = screen.getByLabelText(/status/i) as HTMLSelectElement;
    expect(status.tagName).toBe("SELECT");
    expect(status.options[0].textContent).toMatch(/no change/i);

    // Account select with a "No change" default option.
    const account = screen.getByLabelText(/account/i) as HTMLSelectElement;
    expect(account.tagName).toBe("SELECT");
    expect(account.options[0].textContent).toMatch(/no change/i);

    // Tags input.
    expect(screen.getByLabelText(/add tags/i)).toBeTruthy();

    // Transfer hint paragraph.
    expect(
      screen.getByText(
        /Account and tags are not applied to transfers\. A transfer's category must be a transfer-compatible \(both\) category\./i,
      ),
    ).toBeTruthy();
  });

  it("disables Apply with nothing set and does not call onSubmit", () => {
    const { onSubmit } = renderModal();
    const apply = screen.getByRole("button", { name: /apply/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    fireEvent.click(apply);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("setting only Status=pending submits exactly { status: 'pending' }", () => {
    const { onSubmit } = renderModal();
    const status = screen.getByLabelText(/status/i);
    fireEvent.change(status, { target: { value: "pending" } });

    const apply = screen.getByRole("button", { name: /apply/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ status: "pending" });
  });

  it("setting a Category submits { category_id }", () => {
    const { onSubmit } = renderModal();
    const combo = screen.getByRole("combobox", { name: /category/i });
    fireEvent.focus(combo);
    // Pick "Groceries" (id 11) from the open listbox.
    const listbox = screen.getByRole("listbox");
    fireEvent.click(within(listbox).getByText("Groceries"));

    const apply = screen.getByRole("button", { name: /apply/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ category_id: 11 });
  });
  // TBD-763: batch-set the transaction date and the settlement date.
  it("shows the two date inputs, empty by default", () => {
    renderModal();
    const date = screen.getByLabelText(/^set date$/i) as HTMLInputElement;
    const settled = screen.getByLabelText(/^set settlement date$/i) as HTMLInputElement;
    expect(date.type).toBe("date");
    expect(settled.type).toBe("date");
    expect(date.value).toBe("");
    expect(settled.value).toBe("");
  });

  it("setting only a date submits exactly { date }", () => {
    const { onSubmit } = renderModal();
    fireEvent.change(screen.getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    const apply = screen.getByRole("button", { name: /apply/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    expect(onSubmit).toHaveBeenCalledWith({ date: "2026-05-10" });
  });

  it("status settled + a settlement date submits both", () => {
    const { onSubmit } = renderModal();
    fireEvent.change(screen.getByLabelText(/status/i), { target: { value: "settled" } });
    fireEvent.change(screen.getByLabelText(/^set settlement date$/i), {
      target: { value: "2026-05-07" },
    });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));
    expect(onSubmit).toHaveBeenCalledWith({ status: "settled", settled_date: "2026-05-07" });
  });

  it("warns about CSV re-import duplicates only once a date is set", () => {
    renderModal();
    const copy = /re-importing the same file may duplicate imported transactions you move/i;
    expect(screen.queryByText(copy)).toBeNull();
    fireEvent.change(screen.getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    expect(screen.getByText(copy)).toBeTruthy();
  });

  it("explains which rows each date applies to", () => {
    renderModal();
    expect(
      screen.getByText(/not applied to recurring occurrences or transfers/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/pending ones stay pending and expect to settle on it/i),
    ).toBeTruthy();
  });

  it("clears the dates when the modal reopens", () => {
    const props = {
      count: 3, categories: CATEGORIES, accounts: ACCOUNTS, submitting: false,
      onSubmit: vi.fn(), onCancel: vi.fn(),
    };
    const { rerender } = render(<BatchEditModal open {...props} />);
    fireEvent.change(screen.getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    rerender(<BatchEditModal open={false} {...props} />);
    rerender(<BatchEditModal open {...props} />);
    expect((screen.getByLabelText(/^set date$/i) as HTMLInputElement).value).toBe("");
  });
  it("blocks Apply while the settlement date is before the date", () => {
    const { onSubmit } = renderModal();
    fireEvent.change(screen.getByLabelText(/^set date$/i), {
      target: { value: "2026-05-10" },
    });
    fireEvent.change(screen.getByLabelText(/^set settlement date$/i), {
      target: { value: "2026-05-09" },
    });
    const apply = screen.getByRole("button", { name: /apply/i }) as HTMLButtonElement;
    expect(apply.disabled).toBe(true);
    expect(
      screen.getByText("Settlement date must be on or after the date"),
    ).toBeTruthy();
    fireEvent.click(apply);
    expect(onSubmit).not.toHaveBeenCalled();

    // Same day is fine.
    fireEvent.change(screen.getByLabelText(/^set settlement date$/i), {
      target: { value: "2026-05-10" },
    });
    expect(apply.disabled).toBe(false);
    expect(
      screen.queryByText("Settlement date must be on or after the date"),
    ).toBeNull();
  });
});
