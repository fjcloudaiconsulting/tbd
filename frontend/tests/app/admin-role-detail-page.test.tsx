import { render, screen, waitFor } from "@testing-library/react";

import AdminRoleDetailPage from "@/app/admin/roles/[id]/page";
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
  useParams: () => ({ id: "1" }),
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/roles/1",
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

const ROLE_DETAIL = {
  id: 1,
  slug: "support",
  name: "Support",
  description: "Read-only operator role.",
  is_system_frozen: false,
  permissions: ["audit.view"],
  created_at: "2026-05-07T09:00:00",
  updated_at: "2026-05-07T09:00:00",
};

const CATALOG = {
  namespaces: { audit: ["audit.view"], roles: ["roles.manage"] },
  keys: ["audit.view", "roles.manage"],
};

describe("AdminRoleDetailPage permission gate", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path === `/api/v1/admin/roles/1`) return ROLE_DETAIL;
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });
  });

  it("renders the role detail for a superadmin", async () => {
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRoleDetailPage />);

    await screen.findByDisplayValue("Support");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("redirects a non-superadmin user without roles.manage", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRoleDetailPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries roles.manage in permissions", async () => {
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["roles.manage"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRoleDetailPage />);

    await screen.findByDisplayValue("Support");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });
});
