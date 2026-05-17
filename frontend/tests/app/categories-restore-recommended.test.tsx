import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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

const BASE_USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
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

const EMPTY_CATEGORIES_RESPONSE: unknown[] = [];

function setAuth(role: "owner" | "admin" | "member") {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...BASE_USER, role } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

describe("CategoriesPage Restore recommended button", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("shows the button for org owners", async () => {
    setAuth("owner");
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/categories") return Promise.resolve(EMPTY_CATEGORIES_RESPONSE);
      return Promise.resolve(undefined);
    }) as never);

    render(<CategoriesPage />);
    await waitFor(() => {
      expect(screen.getByTestId("restore-recommended-categories")).toBeInTheDocument();
    });
  });

  it("hides the button for non-owners", async () => {
    setAuth("admin");
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/categories") return Promise.resolve(EMPTY_CATEGORIES_RESPONSE);
      return Promise.resolve(undefined);
    }) as never);

    render(<CategoriesPage />);
    // Wait for the page to settle past its loading spinner. The Edit
    // toggle is unconditional, so its presence signals the header rendered.
    await waitFor(() => {
      expect(screen.getByTestId("categories-edit-toggle")).toBeInTheDocument();
    });
    expect(
      screen.queryByTestId("restore-recommended-categories"),
    ).not.toBeInTheDocument();
  });

  it("posts to the endpoint and surfaces the created count on success", async () => {
    setAuth("owner");
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockImplementation(((url: string, init?: RequestInit) => {
      if (url === "/api/v1/categories" && (!init || init.method === undefined)) {
        return Promise.resolve(EMPTY_CATEGORIES_RESPONSE);
      }
      if (
        url === "/api/v1/categories/restore-recommended"
        && init?.method === "POST"
      ) {
        return Promise.resolve({ created_count: 42 });
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<CategoriesPage />);
    const btn = await screen.findByTestId("restore-recommended-categories");
    fireEvent.click(btn);

    // ConfirmModal renders the "Restore" confirm button.
    const confirmBtn = await screen.findByRole("button", { name: /^restore$/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const successNode = screen.getByTestId("restore-recommended-success");
      expect(successNode.textContent).toMatch(/restored 42/i);
    });

    // The endpoint was called.
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/categories/restore-recommended",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
