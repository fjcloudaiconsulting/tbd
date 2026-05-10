import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AddMasterWithSubsModal from "@/components/ui/AddMasterWithSubsModal";
import { ApiResponseError, apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const cats: Category[] = [
  // Masters.
  {
    id: 1,
    name: "Food",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "food_dining",
    is_system: true,
    transaction_count: 0,
  },
  {
    id: 2,
    name: "Lifestyle",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "lifestyle",
    is_system: true,
    transaction_count: 0,
  },
  {
    id: 3,
    name: "Income",
    type: "income",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "income",
    is_system: true,
    transaction_count: 0,
  },
  {
    id: 4,
    name: "Mixed",
    type: "both",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 0,
  },
  // Subcategories.
  {
    id: 11,
    name: "Restaurants",
    type: "expense",
    parent_id: 1,
    parent_name: "Food",
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 12,
  },
  {
    id: 12,
    name: "Groceries",
    type: "expense",
    parent_id: 1,
    parent_name: "Food",
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 5,
  },
  {
    id: 21,
    name: "Hobbies",
    type: "expense",
    parent_id: 2,
    parent_name: "Lifestyle",
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 0,
  },
  {
    id: 31,
    name: "Salary",
    type: "income",
    parent_id: 3,
    parent_name: "Income",
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 3,
  },
  {
    id: 41,
    name: "Adjustments",
    type: "both",
    parent_id: 4,
    parent_name: "Mixed",
    description: null,
    slug: null,
    is_system: false,
    transaction_count: 0,
  },
];

const newMaster: Category = {
  id: 99,
  name: "Dining out",
  type: "expense",
  parent_id: null,
  parent_name: null,
  description: null,
  slug: null,
  is_system: false,
  transaction_count: 0,
};

describe("AddMasterWithSubsModal", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("renders the modal with name field, type selector, and grouped sub list", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(
      await screen.findByLabelText(/Master name/i),
    ).toBeInTheDocument();

    // Type radios.
    expect(screen.getByRole("radio", { name: /expense/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /income/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /both/i })).toBeInTheDocument();

    // Default type is expense, so Food + Lifestyle groups should be
    // present. Income group must not (its sub Salary is INCOME), and
    // Mixed group must not (its sub Adjustments is BOTH). The backend
    // strict-equal type rule (`sub.type == target.type`) is mirrored
    // in the UI filter.
    expect(screen.getByTestId("group-1-label")).toHaveTextContent("Food");
    expect(screen.getByTestId("group-2-label")).toHaveTextContent("Lifestyle");
    expect(screen.queryByTestId("group-3-label")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-4-label")).not.toBeInTheDocument();

    // Each compatible sub renders a checkbox.
    expect(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Move subcategory Groceries under new master"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Move subcategory Hobbies under new master"),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Move subcategory Salary under new master"),
    ).not.toBeInTheDocument();
    // BOTH-typed subcategory must NOT show under an EXPENSE master.
    expect(
      screen.queryByLabelText("Move subcategory Adjustments under new master"),
    ).not.toBeInTheDocument();
  });

  it("INCOME master only shows INCOME subs (BOTH and EXPENSE filtered out)", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: /income/i }));
    expect(screen.getByTestId("group-3-label")).toHaveTextContent("Income");
    expect(screen.queryByTestId("group-1-label")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-2-label")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-4-label")).not.toBeInTheDocument();
    // BOTH sub Adjustments must NOT appear under an INCOME master.
    expect(
      screen.queryByLabelText("Move subcategory Adjustments under new master"),
    ).not.toBeInTheDocument();
  });

  it("BOTH master only shows BOTH subs (INCOME and EXPENSE filtered out)", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: /both/i }));
    // Only the Mixed group renders; Adjustments (BOTH) is the only
    // compatible sub.
    expect(screen.getByTestId("group-4-label")).toHaveTextContent("Mixed");
    expect(screen.queryByTestId("group-1-label")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-2-label")).not.toBeInTheDocument();
    expect(screen.queryByTestId("group-3-label")).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Move subcategory Adjustments under new master"),
    ).toBeInTheDocument();
    // INCOME and EXPENSE subs are NOT compatible with a BOTH master.
    expect(
      screen.queryByLabelText("Move subcategory Restaurants under new master"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Move subcategory Salary under new master"),
    ).not.toBeInTheDocument();
  });

  it("disables submit when name is empty", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: /Create master/i }),
    ).toBeDisabled();
  });

  it("creates master with no subs selected (no confirm dialog)", async () => {
    apiFetchMock.mockResolvedValueOnce(newMaster as never);
    const onCreated = vi.fn();

    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Create master$/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(newMaster));
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/categories",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "Dining out", type: "expense" }),
      }),
    );
  });

  it("opens a confirm dialog when one or more subs are selected", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    );

    fireEvent.click(
      screen.getByRole("button", { name: /Create master and move/i }),
    );

    // Confirm dialog appears with generic copy (no preview yet because
    // the master hasn't been created). ConfirmModal renders the title
    // as an h3 and the message in a p alongside it.
    expect(
      await screen.findByText(
        /Create "Dining out" and move 1 subcategory\?/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Affected transactions and forecast items/i),
    ).toBeInTheDocument();
    expect(apiFetchMock).not.toHaveBeenCalled();
  });

  it("on confirm Yes: POSTs master then a single atomic batch-move call", async () => {
    apiFetchMock
      .mockResolvedValueOnce(newMaster as never) // POST /categories
      .mockResolvedValueOnce({
        moves: [
          {
            category_id: 11,
            source_master_id: 1,
            target_master_id: 99,
            affected_transaction_count: 12,
            affected_recurring_count: 1,
            affected_forecast_item_count: 0,
            budget_actuals_shifted: true,
          },
          {
            category_id: 21,
            source_master_id: 2,
            target_master_id: 99,
            affected_transaction_count: 0,
            affected_recurring_count: 0,
            affected_forecast_item_count: 1,
            budget_actuals_shifted: false,
          },
        ],
      } as never); // POST /batch-move

    const onCreated = vi.fn();
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    );
    fireEvent.click(
      screen.getByLabelText("Move subcategory Hobbies under new master"),
    );

    fireEvent.click(
      screen.getByRole("button", { name: /Create master and move/i }),
    );

    // ConfirmModal uses role=dialog. Find the dialog containing the
    // Yes button and click it.
    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs.find((d) =>
      within(d).queryByRole("button", { name: /Yes, create and move/i }),
    ) as HTMLElement;
    fireEvent.click(
      within(confirmDialog).getByRole("button", {
        name: /Yes, create and move/i,
      }),
    );

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(newMaster));

    // Exactly two calls: POST master + POST batch-move (NO per-row
    // PATCH loop, NO preview loop).
    const calls = apiFetchMock.mock.calls;
    expect(calls).toHaveLength(2);
    expect(calls[0][0]).toBe("/api/v1/categories");
    expect(calls[0][1]).toMatchObject({ method: "POST" });
    expect(calls[1][0]).toBe("/api/v1/categories/batch-move");
    expect(calls[1][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        moves: [
          { subcategory_id: 11, target_parent_id: 99 },
          { subcategory_id: 21, target_parent_id: 99 },
        ],
      }),
    });
  });

  it("atomic failure (409 name_collision) keeps master and surfaces error; no per-row retry/skip UI", async () => {
    apiFetchMock
      .mockResolvedValueOnce(newMaster as never) // POST master ok
      .mockRejectedValueOnce(
        new ApiResponseError(
          409,
          'Lifestyle already has a subcategory named "Groceries". Rename one before moving.',
          undefined,
          { detail: "name_collision" },
        ),
      ); // POST /batch-move -> 409

    const onCreated = vi.fn();
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    );
    fireEvent.click(
      screen.getByLabelText("Move subcategory Groceries under new master"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Create master and move/i }),
    );

    // ConfirmModal uses role=dialog. Find the dialog containing the
    // Yes button and click it.
    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs.find((d) =>
      within(d).queryByRole("button", { name: /Yes, create and move/i }),
    ) as HTMLElement;
    fireEvent.click(
      within(confirmDialog).getByRole("button", {
        name: /Yes, create and move/i,
      }),
    );

    // Wait for error alert.
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /name conflicts|already has a subcategory|Rename one/i,
      ),
    );

    // Atomic semantics: onCreated is NOT called (no master returned to
    // parent) because nothing moved.
    expect(onCreated).not.toHaveBeenCalled();

    // Master input is locked, button switches to retry.
    expect(
      (screen.getByLabelText(/Master name/i) as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      screen.getByRole("button", { name: /Retry move/i }),
    ).toBeInTheDocument();

    // No per-row failed marker, no "Retry failed moves" / "skip"
    // affordance; the contract is all-or-nothing.
    expect(screen.queryByTestId("sub-failed-12")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Retry failed moves/i }),
    ).not.toBeInTheDocument();

    // Both checkboxes remain selected (nothing moved).
    const restaurantsCheckbox = screen.getByLabelText(
      "Move subcategory Restaurants under new master",
    ) as HTMLInputElement;
    const groceriesCheckbox = screen.getByLabelText(
      "Move subcategory Groceries under new master",
    ) as HTMLInputElement;
    expect(restaurantsCheckbox.checked).toBe(true);
    expect(groceriesCheckbox.checked).toBe(true);
  });

  it("retry path: user adjusts selection and the second batch-move succeeds", async () => {
    apiFetchMock
      .mockResolvedValueOnce(newMaster as never) // POST master ok
      .mockRejectedValueOnce(
        new ApiResponseError(409, "name_collision detail", undefined, {
          detail: "name_collision",
        }),
      ) // first batch-move -> 409
      .mockResolvedValueOnce({ moves: [] } as never); // second batch-move ok

    const onCreated = vi.fn();
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    );
    fireEvent.click(
      screen.getByLabelText("Move subcategory Groceries under new master"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Create master and move/i }),
    );

    // ConfirmModal uses role=dialog. Find the dialog containing the
    // Yes button and click it.
    const dialogs = await screen.findAllByRole("dialog");
    const confirmDialog = dialogs.find((d) =>
      within(d).queryByRole("button", { name: /Yes, create and move/i }),
    ) as HTMLElement;
    fireEvent.click(
      within(confirmDialog).getByRole("button", {
        name: /Yes, create and move/i,
      }),
    );
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    // User unchecks Groceries (the colliding one) and clicks Retry move.
    fireEvent.click(
      screen.getByLabelText("Move subcategory Groceries under new master"),
    );
    fireEvent.click(screen.getByRole("button", { name: /Retry move/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(newMaster));

    // 3 calls total: POST master, batch-move (failed), batch-move (ok).
    const calls = apiFetchMock.mock.calls;
    expect(calls).toHaveLength(3);
    expect(calls[2][0]).toBe("/api/v1/categories/batch-move");
    expect(calls[2][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        moves: [{ subcategory_id: 11, target_parent_id: 99 }],
      }),
    });
  });

  it("surfaces 409 from POST master without committing any moves", async () => {
    apiFetchMock.mockRejectedValueOnce(
      new ApiResponseError(
        409,
        "A master category named Dining out already exists.",
      ),
    );

    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    // No subs selected so we skip the confirm dialog.
    fireEvent.click(screen.getByRole("button", { name: /Create master$/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/already exists/i),
    );
    // Only the POST happened; no batch-move.
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
  });

  it("Escape closes the modal when not submitting and no master created yet", async () => {
    const onCancel = vi.fn();
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={onCancel}
      />,
    );
    await screen.findByLabelText(/Master name/i);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalled();
  });

  it("Cancel button calls onCancel when no master created yet", async () => {
    const onCancel = vi.fn();
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/i }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("awaits async onCreated; reload errors surface inline with retry-refresh", async () => {
    apiFetchMock.mockResolvedValueOnce(newMaster as never);
    let onCreatedCallCount = 0;
    const onCreated = vi.fn(async () => {
      onCreatedCallCount += 1;
      if (onCreatedCallCount === 1) {
        throw new Error("network blip");
      }
    });

    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={onCreated}
        onCancel={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create master$/i }));

    // Reload error surfaces in the alert region with a Retry-refresh
    // button. The modal awaits onCreated rather than dropping the
    // promise on the floor.
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        /failed to refresh/i,
      ),
    );
    expect(screen.getByTestId("retry-refresh")).toBeInTheDocument();
    expect(onCreated).toHaveBeenCalledTimes(1);

    // Clicking Retry refresh re-invokes onCreated; the second call
    // resolves and clears the error.
    fireEvent.click(screen.getByTestId("retry-refresh"));
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(2));
  });

  it("confirm dialog autofocuses its confirm button so keyboard users can reach it", async () => {
    render(
      <AddMasterWithSubsModal
        categories={cats}
        onCreated={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText(/Master name/i), {
      target: { value: "Dining out" },
    });
    fireEvent.click(
      screen.getByLabelText("Move subcategory Restaurants under new master"),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /Create master and move/i }),
    );

    // ConfirmModal autofocuses its confirm button on open. The
    // previous inline alertdialog left focus trapped in the parent
    // modal, making the confirm/cancel buttons unreachable. With
    // ConfirmModal, focus moves to the Yes button automatically.
    const confirmBtn = await screen.findByRole("button", {
      name: /Yes, create and move/i,
    });
    await waitFor(() => expect(document.activeElement).toBe(confirmBtn));
    expect(confirmBtn).not.toBeDisabled();

    // Cancel buttons are present and reachable. The confirm dialog's
    // Cancel and the parent modal's Cancel both have accessible names.
    const cancelBtns = screen.getAllByRole("button", { name: /^Cancel$/i });
    expect(cancelBtns.length).toBeGreaterThanOrEqual(1);
    cancelBtns.forEach((b) => expect(b).not.toBeDisabled());
  });
});
