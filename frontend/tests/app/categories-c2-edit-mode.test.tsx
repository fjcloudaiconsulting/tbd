import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import CategoriesPage from "@/app/categories/page";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/categories",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const CATEGORIES = [
  // Master expense
  {
    id: 100,
    name: "Food",
    slug: "food_dining",
    parent_id: null,
    parent_name: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
  // Subs under Food
  {
    id: 101,
    name: "Restaurants",
    slug: null,
    parent_id: 100,
    parent_name: "Food",
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 5,
  },
  {
    id: 102,
    name: "Groceries",
    slug: null,
    parent_id: 100,
    parent_name: "Food",
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 0,
  },
  // Master expense alt
  {
    id: 200,
    name: "Lifestyle",
    slug: "lifestyle",
    parent_id: null,
    parent_name: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
  // Sub under Lifestyle (used as a target)
  {
    id: 201,
    name: "Entertainment",
    slug: null,
    parent_id: 200,
    parent_name: "Lifestyle",
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 0,
  },
];

function setupApi(
  handlers: Record<string, (init?: RequestInit) => unknown> = {},
) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
      return Promise.resolve(CATEGORIES);
    }
    // Strip query string for the lookup key. Tests can register handlers
    // for both "/api/v1/categories/101?target_category_id=200" (literal)
    // and "/api/v1/categories/101/move/preview" (prefix-style for query
    // strings) and we try the most-specific match first.
    if (handlers[url]) {
      const result = handlers[url](init);
      return result instanceof Promise ? result : Promise.resolve(result);
    }
    const noQuery = url.split("?")[0];
    if (handlers[noQuery]) {
      const result = handlers[noQuery](init);
      return result instanceof Promise ? result : Promise.resolve(result);
    }
    return Promise.resolve({});
  }) as never);
}

describe("CategoriesPage -C2 Edit mode + batch select", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    setupApi();
  });

  it("Edit toggle shows checkboxes on subcategory rows", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    expect(screen.queryByTestId("sub-checkbox-101")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sub-checkbox-102")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    expect(screen.getByTestId("sub-checkbox-101")).toBeInTheDocument();
    expect(screen.getByTestId("sub-checkbox-102")).toBeInTheDocument();
    expect(screen.getByTestId("sub-checkbox-201")).toBeInTheDocument();
  });

  it("Cancel Edit hides checkboxes and clears selection", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    expect(screen.getByTestId("batch-action-bar")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    expect(screen.queryByTestId("sub-checkbox-101")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-action-bar")).not.toBeInTheDocument();
  });

  it("selecting subcategories updates batch-action bar count", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    expect(screen.queryByTestId("batch-action-bar")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    expect(screen.getByTestId("batch-action-count")).toHaveTextContent("1 selected");

    fireEvent.click(screen.getByTestId("sub-checkbox-102"));
    expect(screen.getByTestId("batch-action-count")).toHaveTextContent("2 selected");

    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    expect(screen.getByTestId("batch-action-count")).toHaveTextContent("1 selected");
  });

  it("master rows do not get a checkbox (subcategory-only selection)", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Food")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    expect(screen.queryByTestId("sub-checkbox-100")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sub-checkbox-200")).not.toBeInTheDocument();
  });

  it("individual edit/delete actions remain visible in Edit mode", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));

    const subActions = screen.getByTestId("sub-actions-101");
    const buttons = subActions.querySelectorAll("button");
    expect(buttons).toHaveLength(2);
    expect(subActions.textContent).toContain("Edit");
    expect(subActions.textContent).toContain("Delete");
  });

  it("Esc exits Edit mode when no modal is open", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    expect(screen.getByTestId("sub-checkbox-101")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() =>
      expect(screen.queryByTestId("sub-checkbox-101")).not.toBeInTheDocument(),
    );
  });
});

describe("CategoriesPage -C2 batch move", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("preview aggregates counts across selected subs and confirm clears selection", async () => {
    const previewByCat: Record<number, unknown> = {
      101: {
        category_id: 101,
        source_master_id: 100,
        target_master_id: 200,
        affected_transaction_count: 5,
        affected_recurring_count: 1,
        affected_forecast_item_count: 2,
        budget_actuals_shifted: true,
      },
      102: {
        category_id: 102,
        source_master_id: 100,
        target_master_id: 200,
        affected_transaction_count: 0,
        affected_recurring_count: 0,
        affected_forecast_item_count: 1,
        budget_actuals_shifted: false,
      },
    };

    setupApi({
      "/api/v1/categories/101/move/preview": () => previewByCat[101],
      "/api/v1/categories/102/move/preview": () => previewByCat[102],
      "/api/v1/categories/batch-move": () => ({
        moves: [previewByCat[101], previewByCat[102]],
      }),
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    fireEvent.click(screen.getByTestId("sub-checkbox-102"));
    fireEvent.click(screen.getByTestId("batch-move-button"));

    expect(await screen.findByTestId("batch-move-modal")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("batch-move-target-200"));

    // Aggregate preview: 5 transactions, 1 recurring, 3 forecast items.
    await waitFor(() => {
      const preview = screen.getByTestId("batch-move-preview");
      expect(preview.textContent).toContain("5");
      expect(preview.textContent).toContain("1");
      expect(preview.textContent).toContain("3");
    });

    fireEvent.click(screen.getByTestId("batch-move-confirm"));

    // After success: modal closed, edit mode exited, no checkboxes.
    await waitFor(() => {
      expect(screen.queryByTestId("batch-move-modal")).not.toBeInTheDocument();
      expect(screen.queryByTestId("sub-checkbox-101")).not.toBeInTheDocument();
    });
  });

  it("partial-failure on batch-move surfaces a structured error and keeps the modal open", async () => {
    setupApi({
      "/api/v1/categories/101/move/preview": () => ({
        category_id: 101,
        source_master_id: 100,
        target_master_id: 200,
        affected_transaction_count: 1,
        affected_recurring_count: 0,
        affected_forecast_item_count: 0,
        budget_actuals_shifted: false,
      }),
      "/api/v1/categories/batch-move": () => {
        return Promise.reject(
          new ApiResponseError(
            409,
            "name_collision",
            undefined,
            {
              detail: "name_collision",
              target_parent_id: 200,
              conflicting_child_id: 201,
              conflicting_child_name: "Entertainment",
              normalized_name: "entertainment",
            },
          ),
        );
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    fireEvent.click(screen.getByTestId("batch-move-button"));

    fireEvent.click(await screen.findByTestId("batch-move-target-200"));

    await waitFor(() => {
      expect(screen.getByTestId("batch-move-preview")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("batch-move-confirm"));

    const err = await screen.findByTestId("batch-move-error");
    expect(err.textContent).toContain("Entertainment");
    expect(screen.getByTestId("batch-move-modal")).toBeInTheDocument();
  });

  it("if the post-mutation reload fails, modal stays open and surfaces the refresh-error banner", async () => {
    // Reload-before-close ordering: page must await reload() before
    // closing the modal, so a failure can surface inside the modal.
    // The reload-after-mutation throw is gated on the batch-move POST
    // having already fired, not on a call counter, because StrictMode
    // double-renders inflate the GET count during initial mount.
    let batchMovePosted = false;
    setupApi({
      "/api/v1/categories/101/move/preview": () => ({
        category_id: 101,
        source_master_id: 100,
        target_master_id: 200,
        affected_transaction_count: 5,
        affected_recurring_count: 0,
        affected_forecast_item_count: 0,
        budget_actuals_shifted: false,
      }),
      "/api/v1/categories/batch-move": () => {
        batchMovePosted = true;
        return { moves: [] };
      },
    });

    const originalImpl = vi.mocked(apiFetch).getMockImplementation();
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (
        batchMovePosted
        && url === "/api/v1/categories"
        && (!init || init.method === undefined)
      ) {
        return Promise.reject(new Error("network blip"));
      }
      return originalImpl ? originalImpl(url, init) : Promise.resolve({});
    }) as never);

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    fireEvent.click(screen.getByTestId("batch-move-button"));

    fireEvent.click(await screen.findByTestId("batch-move-target-200"));
    await waitFor(() => {
      expect(screen.getByTestId("batch-move-preview")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("batch-move-confirm"));

    // Modal MUST stay open and show the inline refresh-error banner.
    const banner = await screen.findByTestId("batch-move-refresh-error");
    expect(banner.textContent).toMatch(/network blip/i);
    expect(screen.getByTestId("batch-move-modal")).toBeInTheDocument();
  });
});

describe("CategoriesPage -C2 batch delete", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("aggregate counts surface when subs have dependents", async () => {
    setupApi();
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    // 101 has 5 tx, 102 has 0
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    fireEvent.click(screen.getByTestId("sub-checkbox-102"));
    fireEvent.click(screen.getByTestId("batch-delete-button"));

    const aggregate = await screen.findByTestId("batch-delete-aggregate");
    expect(aggregate.textContent).toContain("1 of 2");
    expect(aggregate.textContent).toContain("5");
  });

  it("loops DELETE per subcategory, surfacing per-row failures with reasons", async () => {
    let deletedCount = 0;
    setupApi({
      "/api/v1/categories/101?target_category_id=200": () => {
        deletedCount += 1;
        // 204 path: apiFetch returns undefined.
        return undefined;
      },
      "/api/v1/categories/102?target_category_id=200": () => {
        return Promise.reject(
          new ApiResponseError(409, "last_in_type", undefined, {
            detail: "last_in_type",
            scope: "subcategory",
            type: "expense",
          }),
        );
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-101"));
    fireEvent.click(screen.getByTestId("sub-checkbox-102"));
    fireEvent.click(screen.getByTestId("batch-delete-button"));

    // C0 spec requires a migration target for ANY dependent (transactions,
    // recurring templates, forecast plan items). The picker is always
    // shown and must be picked for every selected sub.
    const target101 = await screen.findByTestId("batch-delete-target-101");
    fireEvent.change(target101, { target: { value: "200" } });
    const target102 = await screen.findByTestId("batch-delete-target-102");
    fireEvent.change(target102, { target: { value: "200" } });

    fireEvent.click(screen.getByTestId("batch-delete-confirm"));

    const failure = await screen.findByTestId("batch-delete-failure-102");
    expect(failure.textContent).toMatch(/expense/i);

    expect(deletedCount).toBe(1);
    // Modal still open, showing failure row only (101 succeeded and was dropped).
    expect(screen.queryByTestId("batch-delete-row-101")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-delete-row-102")).toBeInTheDocument();
  });

  it("if the post-mutation reload fails after a fully successful delete, modal stays open and surfaces the refresh-error banner", async () => {
    // Reload-before-close ordering: page must await reload() before
    // closing the modal so the failure can surface to the user.
    let deletePosted = false;
    setupApi({
      "/api/v1/categories/102?target_category_id=200": (init) => {
        if (init?.method === "DELETE") {
          deletePosted = true;
        }
        return undefined;
      },
    });
    const originalImpl = vi.mocked(apiFetch).getMockImplementation();
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (
        deletePosted
        && url === "/api/v1/categories"
        && (!init || init.method === undefined)
      ) {
        return Promise.reject(new Error("reload failed"));
      }
      return originalImpl ? originalImpl(url, init) : Promise.resolve({});
    }) as never);

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("categories-edit-toggle"));
    fireEvent.click(screen.getByTestId("sub-checkbox-102"));
    fireEvent.click(screen.getByTestId("batch-delete-button"));

    const target102 = await screen.findByTestId("batch-delete-target-102");
    fireEvent.change(target102, { target: { value: "200" } });

    fireEvent.click(screen.getByTestId("batch-delete-confirm"));

    const banner = await screen.findByTestId("batch-delete-refresh-error");
    expect(banner.textContent).toMatch(/reload failed/i);
    expect(screen.getByTestId("batch-delete-modal")).toBeInTheDocument();
  });

  it("sub-category edit form exposes a description field and PUTs it", async () => {
    const putCalls: Array<{ url: string; body: Record<string, unknown> }> = [];
    setupApi({
      "/api/v1/categories/101": (init) => {
        if (init?.method === "PUT") {
          putCalls.push({
            url: "/api/v1/categories/101",
            body: JSON.parse((init.body as string) ?? "{}"),
          });
          return {};
        }
        return {};
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    const subActions = screen.getByTestId("sub-actions-101");
    const editBtn = Array.from(subActions.querySelectorAll("button")).find(
      (b) => b.textContent === "Edit",
    );
    expect(editBtn).toBeTruthy();
    fireEvent.click(editBtn!);

    // Description input must appear alongside the name input.
    const descInput = await screen.findByPlaceholderText("Hint / description");
    expect(descInput).toBeInTheDocument();
    fireEvent.change(descInput, { target: { value: "Eating out" } });

    // Click Save (button next to the now-visible inputs).
    const editingContainer = descInput.parentElement!;
    const saveBtn = Array.from(editingContainer.querySelectorAll("button")).find(
      (b) => b.textContent === "Save",
    );
    expect(saveBtn).toBeTruthy();
    fireEvent.click(saveBtn!);

    await waitFor(() => expect(putCalls.length).toBe(1));
    expect(putCalls[0].body).toEqual({
      name: "Restaurants",
      description: "Eating out",
    });
  });

  it("editing a top-level category PUTs description (parity with sub-category)", async () => {
    const putCalls: Array<{ url: string; body: Record<string, unknown> }> = [];
    setupApi({
      "/api/v1/categories/100": (init) => {
        if (init?.method === "PUT") {
          putCalls.push({
            url: "/api/v1/categories/100",
            body: JSON.parse((init.body as string) ?? "{}"),
          });
          return {};
        }
        return {};
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Food")).toBeInTheDocument());

    const masterActions = screen.getByTestId("master-actions-100");
    const editBtn = Array.from(masterActions.querySelectorAll("button")).find(
      (b) => b.textContent === "Edit",
    );
    expect(editBtn).toBeTruthy();
    fireEvent.click(editBtn!);

    const descInput = await screen.findByPlaceholderText("Hint / description");
    fireEvent.change(descInput, { target: { value: "All food spending" } });

    const editingContainer = descInput.parentElement!;
    const saveBtn = Array.from(editingContainer.querySelectorAll("button")).find(
      (b) => b.textContent === "Save",
    );
    fireEvent.click(saveBtn!);

    await waitFor(() => expect(putCalls.length).toBe(1));
    expect(putCalls[0].body).toEqual({
      name: "Food",
      description: "All food spending",
    });
  });

  it("editing with empty description sends description: null AND the cleared value renders post-save", async () => {
    // The empty-desc -> null contract has two halves:
    //   1. PUT body must carry `description: null` (so the backend's
    //      Pydantic v2 `model_fields_set` clears the column).
    //   2. After the post-save reload, the UI must reflect the cleared
    //      value, not the pre-save stale string.
    // Both halves matter — only asserting (1) lets the regression
    // re-emerge if the reconciliation path ever drops the new row.
    const putCalls: Array<{ url: string; body: Record<string, unknown> }> = [];
    let descriptionCleared = false;
    const seededCategories = CATEGORIES.map((c) =>
      c.id === 101 ? { ...c, description: "Eating out" } : c,
    );
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
        if (descriptionCleared) {
          return Promise.resolve(
            seededCategories.map((c) =>
              c.id === 101 ? { ...c, description: null } : c,
            ),
          );
        }
        return Promise.resolve(seededCategories);
      }
      if (url === "/api/v1/categories/101" && init?.method === "PUT") {
        const body = JSON.parse((init.body as string) ?? "{}");
        putCalls.push({ url, body });
        if (body.description === null) {
          descriptionCleared = true;
        }
        return Promise.resolve({});
      }
      return Promise.resolve({});
    }) as never);

    render(<CategoriesPage />);
    // Pre-save: the stale description is visible on the row.
    expect(await screen.findByText("Eating out")).toBeInTheDocument();

    const subActions = screen.getByTestId("sub-actions-101");
    const editBtn = Array.from(subActions.querySelectorAll("button")).find(
      (b) => b.textContent === "Edit",
    );
    fireEvent.click(editBtn!);

    // Description input opens pre-populated with the existing value, then
    // gets cleared by the user.
    const descInput = await screen.findByPlaceholderText("Hint / description");
    expect((descInput as HTMLInputElement).value).toBe("Eating out");
    fireEvent.change(descInput, { target: { value: "" } });

    const editingContainer = descInput.parentElement!;
    const saveBtn = Array.from(editingContainer.querySelectorAll("button")).find(
      (b) => b.textContent === "Save",
    );
    fireEvent.click(saveBtn!);

    // Half 1: PUT carried the explicit null.
    await waitFor(() => expect(putCalls.length).toBe(1));
    expect(putCalls[0].body).toEqual({
      name: "Restaurants",
      description: null,
    });

    // Half 2: post-reload UI renders the cleared value — the stale string
    // is gone from the DOM.
    await waitFor(() => {
      expect(screen.queryByText("Eating out")).not.toBeInTheDocument();
    });
    // The sub-category row itself is still present.
    expect(screen.getByText("Restaurants")).toBeInTheDocument();
  });
});
