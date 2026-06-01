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
  {
    id: 101,
    name: "Restaurants",
    slug: null,
    parent_id: 100,
    parent_name: "Food",
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 5, // has transactions -> picker upfront
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
    transaction_count: 0, // no dependents -> direct delete
  },
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

function setupApi(handlers: Record<string, (init?: RequestInit) => unknown> = {}) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
      return Promise.resolve(CATEGORIES);
    }
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

function authReturn() {
  return {
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never;
}

describe("CategoriesPage - single delete with reassign", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useAuth).mockReturnValue(authReturn());
  });

  it("deleting a subcategory with transactions opens the picker and passes target_category_id", async () => {
    const deleteCalls: string[] = [];
    setupApi({
      "/api/v1/categories/101": () => {
        // Should never be called WITHOUT a target for a tx-bearing category.
        deleteCalls.push("no-target");
        return Promise.reject(
          new ApiResponseError(422, "migration target required", undefined, {
            detail: "migration_target_required",
          }),
        );
      },
      "/api/v1/categories/101?target_category_id=200": (init) => {
        deleteCalls.push("with-target");
        expect(init?.method).toBe("DELETE");
        return {
          deleted_category_id: 101,
          migration_target_id: 200,
          migrated_transaction_count: 5,
          migrated_recurring_count: 0,
          migrated_forecast_item_count: 0,
          migrated_rule_count: 0,
          deleted_rule_count: 0,
        };
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Restaurants")).toBeInTheDocument());

    // Click the per-row Delete for Restaurants (id 101).
    fireEvent.click(screen.getByLabelText("Delete Restaurants"));

    // Picker is shown upfront because transaction_count > 0.
    expect(await screen.findByTestId("single-delete-modal")).toBeInTheDocument();
    expect(screen.getByTestId("single-delete-target")).toBeInTheDocument();

    // Confirm is disabled until a target is picked.
    expect(screen.getByTestId("single-delete-confirm")).toBeDisabled();

    fireEvent.change(screen.getByTestId("single-delete-target"), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    await waitFor(() =>
      expect(screen.queryByTestId("single-delete-modal")).not.toBeInTheDocument(),
    );
    // DELETE issued exactly once, with the target.
    expect(deleteCalls).toEqual(["with-target"]);
  });

  it("deleting a no-dependent subcategory deletes directly without a picker", async () => {
    const calls: string[] = [];
    setupApi({
      "/api/v1/categories/102": (init) => {
        calls.push(`delete:${init?.method}`);
        return undefined; // 204
      },
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Groceries")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Delete Groceries"));

    expect(await screen.findByTestId("single-delete-modal")).toBeInTheDocument();
    // No picker for a zero-dependent category.
    expect(screen.queryByTestId("single-delete-target")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    await waitFor(() =>
      expect(screen.queryByTestId("single-delete-modal")).not.toBeInTheDocument(),
    );
    expect(calls).toEqual(["delete:DELETE"]);
  });

  it("a direct delete that returns migration_target_required flips to the picker", async () => {
    let phase = 0;
    setupApi({
      "/api/v1/categories/102": () => {
        phase += 1;
        // First (no-target) call -> backend says a target is required
        // (recurring/forecast dependents the client could not see).
        return Promise.reject(
          new ApiResponseError(422, "migration target required", undefined, {
            detail: "migration_target_required",
          }),
        );
      },
      "/api/v1/categories/102?target_category_id=200": () => ({
        deleted_category_id: 102,
        migration_target_id: 200,
        migrated_transaction_count: 0,
        migrated_recurring_count: 1,
        migrated_forecast_item_count: 0,
        migrated_rule_count: 0,
        deleted_rule_count: 0,
      }),
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Groceries")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Delete Groceries"));
    expect(await screen.findByTestId("single-delete-modal")).toBeInTheDocument();
    // No picker initially (transaction_count === 0).
    expect(screen.queryByTestId("single-delete-target")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    // Backend 422 flips to the picker without a dead-end error.
    expect(await screen.findByTestId("single-delete-target")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("single-delete-target"), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    await waitFor(() =>
      expect(screen.queryByTestId("single-delete-modal")).not.toBeInTheDocument(),
    );
    expect(phase).toBe(1); // the no-target attempt fired exactly once
  });

  it("a has_children 409 shows the human-readable guard message", async () => {
    setupApi({
      "/api/v1/categories/100": () =>
        Promise.reject(
          new ApiResponseError(409, "has children", undefined, {
            detail: "has_children",
            child_names: ["Restaurants", "Groceries"],
          }),
        ),
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Food")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Delete Food"));
    expect(await screen.findByTestId("single-delete-modal")).toBeInTheDocument();
    // Master, transaction_count 0 -> tries direct delete.
    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    const failure = await screen.findByTestId("single-delete-failure");
    expect(failure.textContent).toContain("Has subcategories");
    expect(failure.textContent).toContain("Restaurants");
    // Modal stays open.
    expect(screen.getByTestId("single-delete-modal")).toBeInTheDocument();
  });

  it("a last_in_type 409 shows the floor-invariant message", async () => {
    setupApi({
      "/api/v1/categories/102": () =>
        Promise.reject(
          new ApiResponseError(409, "last in type", undefined, {
            detail: "last_in_type",
            type: "expense",
            scope: "subcategory",
          }),
        ),
    });

    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Groceries")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Delete Groceries"));
    expect(await screen.findByTestId("single-delete-modal")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("single-delete-confirm"));

    const failure = await screen.findByTestId("single-delete-failure");
    expect(failure.textContent).toContain("Cannot delete the only expense subcategory");
  });
});
