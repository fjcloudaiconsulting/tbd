/**
 * Layout test — Categories master list renders as a proportional 2-col grid.
 *
 * Asserts:
 *   - The master-grid container carries the lg:grid-cols-2 class.
 *   - ≥2 master category cards are rendered inside that container.
 *
 * Mock setup mirrors categories-drag-drop.test.tsx.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, beforeEach, it, expect, vi } from "vitest";
import * as React from "react";

import CategoriesPage from "@/app/categories/page";
import { apiFetch } from "@/lib/api";
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

// Seed with 3 master categories (≥2 required by spec) + 1 subcategory.
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
    transaction_count: 5,
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
    id: 300,
    name: "Income",
    slug: "income",
    parent_id: null,
    parent_name: null,
    type: "income" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
];

function setupApi() {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
      return Promise.resolve(CATEGORIES);
    }
    return Promise.resolve({});
  }) as never);
}

describe("CategoriesPage — master list layout (2-col grid)", () => {
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

  it("master-grid uses a responsive masonry (multi-column) layout with ≥2 master cards", async () => {
    render(<CategoriesPage />);
    // Wait for data to load (any master name appears).
    await waitFor(() => expect(screen.getByText("Food")).toBeInTheDocument());

    const grid = screen.getByTestId("categories-master-grid");

    // Masonry via CSS multi-column: 1 column, 2 at lg. Cards are kept whole
    // across columns and the per-child margin provides the vertical gap.
    expect(grid.className).toMatch(/\bcolumns-1\b/);
    expect(grid.className).toMatch(/\blg:columns-2\b/);
    expect(grid.className).toMatch(/break-inside-avoid/);
    // The old fixed 2-col grid left big gaps under short cards — guard against
    // a regression back to it.
    expect(grid.className).not.toMatch(/\bgrid-cols-/);

    // Each master card has data-testid="master-row-{id}" and is a direct child.
    const masterRows = grid.querySelectorAll("[data-testid^='master-row-']");
    expect(masterRows.length).toBeGreaterThanOrEqual(2);
  });
});
