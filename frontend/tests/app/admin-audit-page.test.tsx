import { render, screen, waitFor } from "@testing-library/react";

import AdminAuditPage from "@/app/admin/audit/page";
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
  usePathname: () => "/admin/audit",
  useSearchParams: () => new URLSearchParams(),
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
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const EVENT_LIST = {
  items: [
    {
      id: 7,
      event_type: "org.rename",
      actor_user_id: 1,
      actor_email: "root@platform.io",
      target_org_id: 42,
      target_org_name: "Acme",
      outcome: "success",
      request_id: "req-1234567890ab",
      ip_address: "10.0.0.1",
      created_at: "2026-05-07T09:00:00Z",
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

describe("AdminAuditPage", () => {
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

  it("renders audit events for a superadmin", async () => {
    apiFetchMock.mockResolvedValueOnce(EVENT_LIST as never);

    render(<AdminAuditPage />);

    await screen.findByText("org.rename");
    expect(screen.getByText("root@platform.io")).toBeInTheDocument();
    expect(screen.getByText(/Acme/)).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("redirects non-superadmin users without audit.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminAuditPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries audit.view in permissions", async () => {
    apiFetchMock.mockResolvedValueOnce(EVENT_LIST as never);
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["audit.view"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminAuditPage />);

    await screen.findByText("org.rename");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });
});
