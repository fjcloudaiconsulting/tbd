import { render, screen, waitFor } from "@testing-library/react";

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

const CATEGORIES = [
  // Master with a long name that previously truncated to "D..." at 375px
  {
    id: 100,
    name: "Debt Repayment",
    slug: "debt",
    parent_id: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
  {
    id: 101,
    name: "Credit Card",
    slug: null,
    parent_id: 100,
    type: "expense" as const,
    is_system: false,
    description: null,
    transaction_count: 3,
  },
  {
    id: 102,
    name: "Financial Goals",
    slug: "financial_goals",
    parent_id: null,
    type: "expense" as const,
    is_system: true,
    description: null,
    transaction_count: 0,
  },
];

describe("CategoriesPage — mobile pass", () => {
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
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/categories") return Promise.resolve(CATEGORIES);
      return Promise.resolve({});
    }) as never);
  });

  it("renders the full master category name (no single-character truncation at mobile)", async () => {
    render(<CategoriesPage />);
    // The whole name must be present in the DOM, regardless of viewport.
    // The visual truncation under sm: must not chop the underlying text node.
    await waitFor(() => expect(screen.getByText("Debt Repayment")).toBeInTheDocument());
    expect(screen.getByText("Financial Goals")).toBeInTheDocument();
  });

  it("master row reflows actions to a second line below sm breakpoint", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Debt Repayment")).toBeInTheDocument());
    const row = screen.getByTestId("master-row-100");
    // Mobile: column stack; sm+: row layout. The row container drives the wrap.
    expect(row.className).toContain("flex-col");
    expect(row.className).toContain("sm:flex-row");
  });

  it("master action group keeps all three actions visible (no kebab hide)", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Debt Repayment")).toBeInTheDocument());
    const actions = screen.getByTestId("master-actions-100");
    // All three actions present and tappable.
    expect(actions.querySelectorAll("button")).toHaveLength(3);
    expect(actions.textContent).toContain("+ Add Sub");
    expect(actions.textContent).toContain("Edit");
    expect(actions.textContent).toContain("Delete");
  });

  it("subcategory Edit/Delete buttons have a 44px mobile hit area", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Credit Card")).toBeInTheDocument());
    const subActions = screen.getByTestId("sub-actions-101");
    const buttons = subActions.querySelectorAll("button");
    expect(buttons).toHaveLength(2);
    for (const btn of Array.from(buttons)) {
      // Hit area is achieved via min-h-[44px] + min-w-[44px] + px-2 padding
      // which collapses back to compact desktop sizing at md:.
      expect(btn.className).toContain("min-h-[44px]");
      expect(btn.className).toContain("min-w-[44px]");
      expect(btn.className).toContain("md:min-h-0");
      expect(btn.className).toContain("md:min-w-0");
    }
  });

  it("subcategory Delete button keeps its accessible name", async () => {
    render(<CategoriesPage />);
    await waitFor(() => expect(screen.getByText("Credit Card")).toBeInTheDocument());
    // aria-label preserved so screen readers still get a per-row label.
    expect(screen.getByLabelText("Delete Credit Card")).toBeInTheDocument();
  });
});
