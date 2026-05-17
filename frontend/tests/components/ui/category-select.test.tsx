import { fireEvent, render, screen, within } from "@testing-library/react";

import CategorySelect from "@/components/ui/CategorySelect";
import type { Category } from "@/lib/types";

vi.mock("@/components/ui/AddCategoryModal", () => ({
  default: (props: {
    initialName: string;
    initialType: "income" | "expense" | "both";
    masterCategories: Category[];
    lockedType?: "income" | "expense";
    onCreated: (cat: Category) => void;
    onCancel: () => void;
  }) => (
    <div data-testid="add-category-modal-stub">
      <span data-testid="modal-initial-type">{props.initialType}</span>
      <span data-testid="modal-locked-type">{props.lockedType ?? ""}</span>
      <span data-testid="modal-master-count">
        {props.masterCategories.length}
      </span>
      <button type="button" onClick={() => props.onCancel()}>
        stub-cancel
      </button>
      {/* Emit category-created with a specific type so tests can
          exercise both the compatible and the bothOnly-incompatible
          code paths added in the PR #296 architect-feedback round. */}
      <button
        type="button"
        onClick={() =>
          props.onCreated({
            id: 9999,
            name: props.initialName || "Stub Expense",
            type: "expense",
            parent_id: null,
            parent_name: null,
            description: null,
            slug: "stub-expense",
            is_system: false,
            transaction_count: 0,
          })
        }
      >
        stub-create-expense
      </button>
      <button
        type="button"
        onClick={() =>
          props.onCreated({
            id: 8888,
            name: props.initialName || "Stub Transfer",
            type: "both",
            parent_id: null,
            parent_name: null,
            description: null,
            slug: "stub-transfer",
            is_system: false,
            transaction_count: 0,
          })
        }
      >
        stub-create-both
      </button>
    </div>
  ),
}));

const CATEGORIES: Category[] = [
  // Income master
  {
    id: 10,
    name: "Salary",
    type: "income",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "salary",
    is_system: false,
    transaction_count: 0,
  },
  // Expense master
  {
    id: 20,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
  // Both-typed master (transfers)
  {
    id: 30,
    name: "Transfer",
    type: "both",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "transfer",
    is_system: false,
    transaction_count: 0,
  },
  // Expense subcategory
  {
    id: 21,
    name: "Supermarket",
    type: "expense",
    parent_id: 20,
    parent_name: "Groceries",
    description: null,
    slug: "supermarket",
    is_system: false,
    transaction_count: 0,
  },
];

describe("CategorySelect — value resolution under filterType", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("does not display an incompatible category as the selected chip when filterType narrows the type", () => {
    // value points at the income master "Salary" (id 10), but the
    // dropdown is locked to expense. The combobox must render empty
    // (no stale "Salary" chip), since the resolved value is
    // incompatible with the active filterType.
    render(
      <CategorySelect
        id="t1"
        categories={CATEGORIES}
        value={10}
        onChange={vi.fn()}
        filterType="expense"
      />,
    );

    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("");
  });

  it("still displays a compatible value (same type)", () => {
    render(
      <CategorySelect
        id="t2"
        categories={CATEGORIES}
        value={20}
        onChange={vi.fn()}
        filterType="expense"
      />,
    );
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("Groceries");
  });

  it("treats BOTH-typed categories as compatible with any filterType", () => {
    // "Transfer" (type: both) is compatible with both expense and income
    // selectors — should still render as the chosen value.
    render(
      <CategorySelect
        id="t3"
        categories={CATEGORIES}
        value={30}
        onChange={vi.fn()}
        filterType="expense"
      />,
    );
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("Transfer");
  });

  it("dropdown filterType=expense includes BOTH-typed masters alongside expense ones", () => {
    render(
      <CategorySelect
        id="t4"
        categories={CATEGORIES}
        value=""
        onChange={vi.fn()}
        filterType="expense"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    const listbox = screen.getByRole("listbox");
    expect(within(listbox).getByText("Groceries")).toBeInTheDocument();
    expect(within(listbox).getByText("Supermarket")).toBeInTheDocument();
    expect(within(listbox).getByText("Transfer")).toBeInTheDocument();
    expect(within(listbox).queryByText("Salary")).not.toBeInTheDocument();
  });

  it("forwards filterType to AddCategoryModal as lockedType when set", () => {
    render(
      <CategorySelect
        id="t5"
        categories={CATEGORIES}
        value=""
        onChange={vi.fn()}
        filterType="income"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("modal-locked-type")).toHaveTextContent(
      "income",
    );
  });

  it("does not pass lockedType when filterType is unset (free-form fallback)", () => {
    render(
      <CategorySelect
        id="t6"
        categories={CATEGORIES}
        value=""
        onChange={vi.fn()}
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("modal-locked-type")).toHaveTextContent("");
  });

  it("typeFilter=BOTH narrows the dropdown to type=both categories only", () => {
    // Transfer context: the only acceptable category type on the shared
    // transfer leg is `both`. Income- and expense-only options must
    // disappear, including their subcategories.
    render(
      <CategorySelect
        id="t7"
        categories={CATEGORIES}
        value=""
        onChange={vi.fn()}
        typeFilter="BOTH"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    const listbox = screen.getByRole("listbox");
    expect(within(listbox).getByText("Transfer")).toBeInTheDocument();
    expect(within(listbox).queryByText("Salary")).not.toBeInTheDocument();
    expect(within(listbox).queryByText("Groceries")).not.toBeInTheDocument();
    expect(within(listbox).queryByText("Supermarket")).not.toBeInTheDocument();
  });

  it("typeFilter=BOTH hides a stale value that points at an incompatible category", () => {
    // The picker is now type=both only, but value still points at the
    // expense master (id 20). The selected-chip must clear (input is
    // empty), the same way `filterType` does for income/expense.
    render(
      <CategorySelect
        id="t8"
        categories={CATEGORIES}
        value={20}
        onChange={vi.fn()}
        typeFilter="BOTH"
      />,
    );
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("");
  });

  it("typeFilter=BOTH keeps a compatible (type=both) value visible as the selected chip", () => {
    render(
      <CategorySelect
        id="t9"
        categories={CATEGORIES}
        value={30}
        onChange={vi.fn()}
        typeFilter="BOTH"
      />,
    );
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("Transfer");
  });

  it("typeFilter=BOTH defaults the AddCategoryModal to initialType=both without locking it", () => {
    // The user must be able to override the type from inside the modal
    // if they realize the picker context was wrong; locking it would
    // hide the radio group. Only income/expense lock today.
    render(
      <CategorySelect
        id="t10"
        categories={CATEGORIES}
        value=""
        onChange={vi.fn()}
        typeFilter="BOTH"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("modal-initial-type")).toHaveTextContent("both");
    expect(screen.getByTestId("modal-locked-type")).toHaveTextContent("");
  });

  // Architect feedback on PR #296: in bothOnly mode the modal's type
  // radio is unlocked, so a user can flip to income/expense. Today the
  // resulting category lands as the selected value via handleSelect,
  // even though the picker would later hide it. The form then submits
  // an id the backend rejects. The fix is to gate handleSelect on
  // compatibility — onCategoryCreated still fires (so other pickers
  // can list the new category), but onChange must NOT fire when the
  // returned category is incompatible with the active typeFilter.
  it("typeFilter=BOTH: an incompatible (expense) created category is not selected", () => {
    const onChange = vi.fn();
    const onCategoryCreated = vi.fn();
    render(
      <CategorySelect
        id="t11"
        categories={CATEGORIES}
        value=""
        onChange={onChange}
        onCategoryCreated={onCategoryCreated}
        typeFilter="BOTH"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    fireEvent.click(screen.getByText("stub-create-expense"));

    // Upward notification fires regardless of compatibility, so other
    // forms (e.g. /transactions in income/expense mode) can list it.
    expect(onCategoryCreated).toHaveBeenCalledTimes(1);
    expect(onCategoryCreated).toHaveBeenCalledWith(
      expect.objectContaining({ type: "expense" }),
    );

    // Load-bearing assertion: the picker does NOT select the
    // incompatible category. Before the fix this fired once with id 9999.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("typeFilter=BOTH: a compatible (both) created category IS selected", () => {
    const onChange = vi.fn();
    const onCategoryCreated = vi.fn();
    render(
      <CategorySelect
        id="t12"
        categories={CATEGORIES}
        value=""
        onChange={onChange}
        onCategoryCreated={onCategoryCreated}
        typeFilter="BOTH"
      />,
    );
    fireEvent.focus(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    fireEvent.click(screen.getByText("stub-create-both"));

    expect(onCategoryCreated).toHaveBeenCalledTimes(1);
    // Compatible: picker selects it as the new value.
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(8888);
  });
});
