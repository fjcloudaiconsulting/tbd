import { render, screen, waitFor } from "@testing-library/react";

import AdminUserDetailPage from "@/app/admin/users/[user_id]/page";
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

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/users/42",
  useParams: () => ({ user_id: "42" }),
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
};

const SAMPLE_DETAIL = {
  id: 42,
  email: "ada@acme.io",
  username: "ada",
  display_name: "Ada Lovelace",
  is_superadmin: false,
  is_active: true,
  email_verified: true,
  mfa_enabled: true,
  password_set: true,
  password_changed_at: "2026-04-30T10:00:00",
  sessions_invalidated_at: null,
  onboarded_at: "2026-04-15T10:00:00",
  created_at: "2026-04-15T10:00:00",
  phone: null,
  orgs: [{ org_id: 10, name: "Acme Co", role: "owner" }],
  recent_audit_events: [
    {
      id: 1,
      event_type: "admin.org.subscription.override",
      outcome: "success",
      target_org_id: 11,
      target_org_name: "Beta",
      created_at: "2026-05-12T15:00:00",
    },
  ],
};

describe("AdminUserDetailPage", () => {
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

  it("renders the user identity card and org membership", async () => {
    apiFetchMock.mockResolvedValueOnce(SAMPLE_DETAIL as never);

    render(<AdminUserDetailPage />);

    // Page title is the display name.
    await screen.findByRole("heading", { name: "Ada Lovelace" });
    // Identity fields present.
    expect(screen.getByText("ada@acme.io")).toBeInTheDocument();
    expect(screen.getByText("ada")).toBeInTheDocument();
    // Org membership link.
    expect(screen.getByRole("link", { name: "Acme Co" })).toHaveAttribute(
      "href",
      "/admin/orgs/10",
    );
    // Recent audit event row.
    expect(screen.getByText("admin.org.subscription.override")).toBeInTheDocument();
  });

  it("redirects non-superadmin users without users.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminUserDetailPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("shows an error banner on failed fetch", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    render(<AdminUserDetailPage />);

    await screen.findByRole("alert");
  });
});
