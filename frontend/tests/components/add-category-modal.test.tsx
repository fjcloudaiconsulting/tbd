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

  it("focuses the name field on mount (after portal renders)", async () => {
    render(
      <AddCategoryModal
        initialName="Rent"
        initialType="expense"
        masterCategories={masterCategories}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    // The modal gates rendering on `mounted`, which flips in a
    // post-mount effect. Wait for the input to be present (proving
    // the portal mounted), then assert it's the active element.
    const nameInput = await screen.findByLabelText(/Name/i);
    await waitFor(() => {
      expect(document.activeElement).toBe(nameInput);
    });
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

  describe("lockedType", () => {
    // Income master + expense master + a "both"-typed master, so we can
    // assert the parent dropdown filters by compatibility.
    const mixedMasters: Category[] = [
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
    ];

    it("hides the free Type radio group when lockedType is provided", () => {
      render(
        <AddCategoryModal
          initialName="Side gig"
          initialType="income"
          lockedType="income"
          masterCategories={mixedMasters}
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />,
      );
      // The free radio group should be hidden. The "Expense" / "Both"
      // radio inputs must not be present.
      expect(
        screen.queryByRole("radio", { name: /expense/i }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("radio", { name: /both/i }),
      ).not.toBeInTheDocument();
    });

    it("filters the parent-master dropdown to compatible masters when lockedType=income", () => {
      render(
        <AddCategoryModal
          initialName="Bonus"
          initialType="income"
          lockedType="income"
          masterCategories={mixedMasters}
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />,
      );
      // Toggle subcategory so the parent dropdown is rendered.
      fireEvent.click(screen.getByLabelText(/Subcategory/i));
      const parentSelect = screen.getByLabelText(
        /Parent category/i,
      ) as HTMLSelectElement;
      // lockedType=income excludes both expense AND "both" masters: child
      // categories inherit parent type at the backend, so allowing a "both"
      // parent would silently create a "both" child against the modal's
      // "income only" promise.
      const optionTexts = Array.from(parentSelect.options).map(
        (o) => o.textContent ?? "",
      );
      expect(optionTexts).toContain("Salary");
      expect(optionTexts).not.toContain("Transfer");
      expect(optionTexts).not.toContain("Groceries");
    });

    it("submits POST with the locked type even if initialType disagrees", async () => {
      const created: Category = {
        id: 70,
        name: "Refund",
        type: "income",
        parent_id: null,
        parent_name: null,
        description: null,
        slug: "refund",
        is_system: false,
        transaction_count: 0,
      };
      apiFetchMock.mockResolvedValueOnce(created as never);
      const onCreated = vi.fn();

      render(
        <AddCategoryModal
          initialName="Refund"
          // initialType differs from lockedType to prove the lock wins.
          initialType="expense"
          lockedType="income"
          masterCategories={mixedMasters}
          onCreated={onCreated}
          onCancel={vi.fn()}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /Add category/i }));

      await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/categories",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ name: "Refund", type: "income" }),
        }),
      );
    });

    it("free-form behavior is unchanged when lockedType is unset", () => {
      render(
        <AddCategoryModal
          initialName="Coffee"
          initialType="expense"
          masterCategories={masterCategories}
          onCreated={vi.fn()}
          onCancel={vi.fn()}
        />,
      );
      // All three radio options remain user-pickable.
      expect(
        screen.getByRole("radio", { name: /expense/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("radio", { name: /income/i }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("radio", { name: /both/i }),
      ).toBeInTheDocument();
    });
  });
});
