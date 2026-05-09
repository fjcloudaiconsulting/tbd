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

describe("CategoriesPage — C2 Edit mode + batch select", () => {
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

describe("CategoriesPage — C2 batch move", () => {
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
});

describe("CategoriesPage — C2 batch delete", () => {
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
      "/api/v1/categories/102": () => {
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

    // 101 needs a target picker since transaction_count > 0; pick Lifestyle (200).
    const targetSelect = await screen.findByTestId("batch-delete-target-101");
    fireEvent.change(targetSelect, { target: { value: "200" } });

    fireEvent.click(screen.getByTestId("batch-delete-confirm"));

    const failure = await screen.findByTestId("batch-delete-failure-102");
    expect(failure.textContent).toMatch(/expense/i);

    expect(deletedCount).toBe(1);
    // Modal still open, showing failure row only (101 succeeded and was dropped).
    expect(screen.queryByTestId("batch-delete-row-101")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-delete-row-102")).toBeInTheDocument();
  });
});
