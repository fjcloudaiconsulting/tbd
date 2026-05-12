import { render, screen, waitFor } from "@testing-library/react";

import AdminAnalyticsPage from "@/app/admin/analytics/page";
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
  usePathname: () => "/admin/analytics",
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

const POPULATED_RESPONSE = {
  window_days: 30,
  generated_at: "2026-05-11T14:00:00Z",
  logins_by_day: [
    { date: "2026-05-10", count: 3 },
    { date: "2026-05-11", count: 5 },
  ],
  tx_writes_by_day: [
    { date: "2026-05-10", count: 12 },
    { date: "2026-05-11", count: 14 },
  ],
  imports_by_day: [
    { date: "2026-05-10", count: 1 },
    { date: "2026-05-11", count: 0 },
  ],
  top_orgs_by_tx_volume: [
    { rank: 1, org_id: 42, org_name: "Acme", tx_count: 26 },
  ],
  dormant_orgs: [
    {
      org_id: 99,
      org_name: "Silent Co",
      last_tx_at: null,
      days_since_last_activity: null,
    },
  ],
};

const EMPTY_RESPONSE = {
  window_days: 30,
  generated_at: "2026-05-11T14:00:00Z",
  logins_by_day: [{ date: "2026-05-11", count: 0 }],
  tx_writes_by_day: [{ date: "2026-05-11", count: 0 }],
  imports_by_day: [{ date: "2026-05-11", count: 0 }],
  top_orgs_by_tx_volume: [],
  dormant_orgs: [],
};

describe("AdminAnalyticsPage", () => {
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

  it("renders the analytics envelope for a superadmin", async () => {
    apiFetchMock.mockResolvedValueOnce(POPULATED_RESPONSE as never);

    render(<AdminAnalyticsPage />);

    await screen.findByText(/Successful logins/i);
    expect(screen.getByText(/Transactions created/i)).toBeInTheDocument();
    expect(screen.getByText(/Rows imported/i)).toBeInTheDocument();
    expect(screen.getByText(/Acme/)).toBeInTheDocument();
    expect(screen.getByText(/Silent Co/)).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("renders empty-state copy when no activity is present", async () => {
    apiFetchMock.mockResolvedValueOnce(EMPTY_RESPONSE as never);

    render(<AdminAnalyticsPage />);

    await waitFor(() => {
      expect(
        screen.getByText(/No transaction activity in the window\./i),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/All organizations are active\./i),
      ).toBeInTheDocument();
    });
  });

  it("redirects non-superadmin users without analytics.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminAnalyticsPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries analytics.view in permissions", async () => {
    apiFetchMock.mockResolvedValueOnce(POPULATED_RESPONSE as never);
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["analytics.view"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminAnalyticsPage />);

    await screen.findByText(/Successful logins/i);
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });
});
