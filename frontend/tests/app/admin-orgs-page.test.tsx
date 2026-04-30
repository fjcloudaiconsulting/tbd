import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AdminOrgsPage from "@/app/admin/orgs/page";
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
  return { ...actual, useAuth: vi.fn(), AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/orgs",
}));


const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

describe("AdminOrgsPage", () => {
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

  it("renders the orgs table from the API", async () => {
    apiFetchMock.mockResolvedValueOnce({
      items: [
        {
          id: 10, name: "Acme", plan_slug: "free",
          subscription_status: "trialing", trial_end: "2026-05-15",
          user_count: 3, active_user_count: 2,
          created_at: "2026-04-15T10:00:00",
          last_user_created_at: "2026-04-30T10:00:00",
        },
      ],
      total: 1, limit: 50, offset: 0,
    } as never);

    render(<AdminOrgsPage />);

    await screen.findByText("Acme");
    expect(screen.getByText("free")).toBeInTheDocument();
    expect(screen.getByText("trialing")).toBeInTheDocument();
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("redirects non-superadmin users away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });

    render(<AdminOrgsPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });
});
