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
});
