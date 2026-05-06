import { fireEvent, render, screen } from "@testing-library/react";

import CategorySelect from "@/components/ui/CategorySelect";
import type { Category } from "@/lib/types";

vi.mock("@/components/ui/AddCategoryModal", () => ({
  default: (props: {
    initialName: string;
    initialType: "income" | "expense" | "both";
    masterCategories: Category[];
    onCreated: (cat: Category) => void;
    onCancel: () => void;
  }) => {
    return (
      <div data-testid="add-category-modal-stub">
        <span data-testid="modal-initial-name">{props.initialName}</span>
        <span data-testid="modal-initial-type">{props.initialType}</span>
        <button
          type="button"
          onClick={() =>
            props.onCreated({
              id: 999,
              name: "NewCat",
              type: "expense",
              parent_id: null,
              parent_name: null,
              description: null,
              slug: "newcat",
              is_system: false,
              transaction_count: 0,
            })
          }
        >
          stub-created
        </button>
        <button type="button" onClick={() => props.onCancel()}>
          stub-cancel
        </button>
      </div>
    );
  },
}));

const categories: Category[] = [
  {
    id: 1,
    name: "Housing",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "housing",
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 2,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
];

function openDropdown() {
  const input = screen.getByRole("combobox");
  fireEvent.focus(input);
  return input;
}

describe("CategorySelect Add category affordance", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders + Add category at the bottom of the open dropdown", () => {
    render(
      <CategorySelect
        id="t1"
        categories={categories}
        value=""
        onChange={vi.fn()}
      />
    );
    openDropdown();
    expect(
      screen.getByRole("button", { name: /Add category/i })
    ).toBeInTheDocument();
  });

  it("pre-fills modal name with current query text", () => {
    render(
      <CategorySelect
        id="t2"
        categories={categories}
        value=""
        onChange={vi.fn()}
      />
    );
    const input = openDropdown();
    fireEvent.change(input, { target: { value: "Rent" } });
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("modal-initial-name")).toHaveTextContent("Rent");
  });

  it("calls onCategoryCreated and selects the new category on success", () => {
    const onChange = vi.fn();
    const onCategoryCreated = vi.fn();
    render(
      <CategorySelect
        id="t3"
        categories={categories}
        value=""
        onChange={onChange}
        onCategoryCreated={onCategoryCreated}
      />
    );
    openDropdown();
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    fireEvent.click(screen.getByText("stub-created"));

    expect(onCategoryCreated).toHaveBeenCalledWith(
      expect.objectContaining({ id: 999, name: "NewCat" })
    );
    expect(onChange).toHaveBeenCalledWith(999);
  });

  it("on modal cancel, closes the modal and combobox stays open", () => {
    render(
      <CategorySelect
        id="t4"
        categories={categories}
        value=""
        onChange={vi.fn()}
      />
    );
    openDropdown();
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("add-category-modal-stub")).toBeInTheDocument();

    fireEvent.click(screen.getByText("stub-cancel"));
    expect(
      screen.queryByTestId("add-category-modal-stub")
    ).not.toBeInTheDocument();
    // Dropdown still open: the Add category button is still rendered.
    expect(
      screen.getByRole("button", { name: /Add category/i })
    ).toBeInTheDocument();
  });

  it("uses filterType to set initial modal type", () => {
    render(
      <CategorySelect
        id="t5"
        categories={categories}
        value=""
        onChange={vi.fn()}
        filterType="income"
      />
    );
    openDropdown();
    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));
    expect(screen.getByTestId("modal-initial-type")).toHaveTextContent(
      "income"
    );
  });
});
