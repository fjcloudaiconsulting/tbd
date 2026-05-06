import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AddCategoryModal from "@/components/ui/AddCategoryModal";
import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const masterCategories: Category[] = [
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
    name: "Food",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "food",
    is_system: false,
    transaction_count: 0,
  },
];

describe("AddCategoryModal", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders with the name field pre-filled from initialName", () => {
    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    const nameInput = screen.getByLabelText(/Name/i) as HTMLInputElement;
    expect(nameInput.value).toBe("Rent");
  });

  it("disables Add category when name is empty", () => {
    render(
      <AddCategoryModal
        initialName=""
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(
      screen.getByRole("button", { name: /Add category/i })
    ).toBeDisabled();
  });

  it("disables Add category while submitting", async () => {
    let resolveFetch: (value: Category) => void = () => {};
    apiFetchMock.mockImplementationOnce(
      () =>
        new Promise<Category>((resolve) => {
          resolveFetch = resolve;
        }) as never
    );

    render(
      <AddCategoryModal
        initialName="Coffee"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    const submit = screen.getByRole("button", { name: /Add category/i });
    fireEvent.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());
    expect(submit).toHaveTextContent(/Adding/i);

    // Resolve to clean up.
    resolveFetch({
      id: 99,
      name: "Coffee",
      type: "expense",
      parent_id: null,
      parent_name: null,
      description: null,
      slug: "coffee",
      is_system: false,
      transaction_count: 0,
    });
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalled());
  });

  it("submits POST with master category body (no parent_id)", async () => {
    const created: Category = {
      id: 50,
      name: "Rent",
      type: "expense",
      parent_id: null,
      parent_name: null,
      description: null,
      slug: "rent",
      is_system: false,
      transaction_count: 0,
    };
    apiFetchMock.mockResolvedValueOnce(created as never);
    const onCreated = vi.fn();

    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/categories",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "Rent", type: "expense" }),
      })
    );
  });

  it("submits POST with parent_id when subcategory and parent are selected", async () => {
    const created: Category = {
      id: 60,
      name: "Mortgage",
      type: "expense",
      parent_id: 1,
      parent_name: "Housing",
      description: null,
      slug: "mortgage",
      is_system: false,
      transaction_count: 0,
    };
    apiFetchMock.mockResolvedValueOnce(created as never);
    const onCreated = vi.fn();

    render(
      <AddCategoryModal
        initialName="Mortgage"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />
    );

    fireEvent.click(screen.getByLabelText(/Subcategory/i));
    const parentSelect = screen.getByLabelText(
      /Parent category/i
    ) as HTMLSelectElement;
    fireEvent.change(parentSelect, { target: { value: "1" } });

    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/categories",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          name: "Mortgage",
          type: "expense",
          parent_id: 1,
        }),
      })
    );
  });

  it("shows API error message via extractErrorMessage", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("Category already exists"));

    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Add category/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /Category already exists/i
      )
    );
  });

  it("Cancel calls onCancel without submitting", () => {
    const onCancel = vi.fn();
    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /Cancel/i }));
    expect(onCancel).toHaveBeenCalled();
    expect(apiFetchMock).not.toHaveBeenCalled();
  });

  it("disables Add category and shows hint when subcategory is checked but no parent picked", () => {
    render(
      <AddCategoryModal
        initialName="Mortgage"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    // With a name filled in but no subcategory toggle, submit is enabled.
    const submit = screen.getByRole("button", { name: /Add category/i });
    expect(submit).not.toBeDisabled();

    // Toggling Subcategory (without picking a parent) must disable submit
    // and surface the inline hint.
    fireEvent.click(screen.getByLabelText(/Subcategory/i));
    expect(submit).toBeDisabled();
    expect(screen.getByText(/Pick a parent category/i)).toBeInTheDocument();

    // Selecting a parent re-enables submit and removes the hint.
    fireEvent.change(screen.getByLabelText(/Parent category/i), {
      target: { value: "1" },
    });
    expect(submit).not.toBeDisabled();
    expect(
      screen.queryByText(/Pick a parent category/i)
    ).not.toBeInTheDocument();
  });

  it("Escape key calls onCancel", () => {
    const onCancel = vi.fn();
    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={onCancel}
      />
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalled();
  });
});
