import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AdminRolesPage from "@/app/admin/roles/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
      "@/components/auth/AuthProvider",
    );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/roles",
}));

const SUPERADMIN = {
  id: 1,
  username: "root",
  email: "root@platform.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Platform",
  billing_cycle_day: 1,
  is_superadmin: true,
  is_active: true,
  mfa_enabled: false,
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const CATALOG = {
  namespaces: {
    admin: ["admin.view"],
    audit: ["audit.view"],
    orgs: ["orgs.manage", "orgs.view"],
    plans: ["plans.manage"],
    roles: ["roles.manage"],
  },
  keys: [
    "admin.view",
    "audit.view",
    "orgs.manage",
    "orgs.view",
    "plans.manage",
    "roles.manage",
  ],
};

describe("AdminRolesPage", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("renders the seeded superadmin role with system badge", async () => {
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/admin/roles") {
        return {
          items: [
            {
              id: 1,
              slug: "superadmin",
              name: "Superadmin",
              description: "Full platform access.",
              is_system_frozen: true,
              permission_count: 6,
              created_at: "2026-05-07T09:00:00",
              updated_at: "2026-05-07T09:00:00",
            },
          ],
        };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    await screen.findByText("Superadmin");
    expect(screen.getByText("system")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
  });

  it("redirects non-superadmin users away", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRolesPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("opens the create modal and submits a new role", async () => {
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/v1/admin/roles" && (!init || init.method === undefined)) {
        return { items: [] };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      if (path === "/api/v1/admin/roles" && init?.method === "POST") {
        const body = JSON.parse(init.body as string);
        expect(body.slug).toBe("support");
        expect(body.name).toBe("Support");
        expect(body.permissions).toEqual(["admin.view"]);
        return {
          id: 2,
          slug: "support",
          name: "Support",
          description: null,
          is_system_frozen: false,
          permissions: ["admin.view"],
          created_at: "2026-05-07T09:00:00",
          updated_at: "2026-05-07T09:00:00",
        };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    // Wait for the load to complete (button enables once catalog arrives).
    const newButton = await screen.findByRole("button", { name: /new role/i });
    await waitFor(() => expect(newButton).not.toBeDisabled());
    fireEvent.click(newButton);

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/slug/i), {
      target: { value: "support" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), {
      target: { value: "Support" },
    });
    // Toggle the admin.view permission checkbox.
    fireEvent.click(within(dialog).getByLabelText(/admin\.view/i));

    fireEvent.click(within(dialog).getByRole("button", { name: /create role/i }));

    await waitFor(() => {
      // Modal closed.
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
