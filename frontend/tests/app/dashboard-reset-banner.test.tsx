import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
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
const pushMock = vi.fn();
// Return a stable router object so useEffect deps don't re-trigger
// the banner-show effect on every render after a click.
const stableRouter = { push: pushMock, replace: replaceMock };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1,
  is_superadmin: false, is_active: true, mfa_enabled: false,
  subscription_status: null, subscription_plan: null, trial_end: null,
};

function mockEmptyDashboard() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets") return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle") return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period") return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods") return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/transactions")) return Promise.resolve([]);
    if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
    return Promise.resolve({});
  }) as never);
}

describe("DashboardPage — reset banner", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    replaceMock.mockReset();
    // Reset URL between tests; the page reads window.location.search
    // directly so the banner-show effect responds to whatever the URL
    // is at first render.
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    mockEmptyDashboard();
  });

  it("does not render the banner without ?reset=1", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.queryByTestId("reset-banner")).toBeNull());
  });

  it("renders the banner when ?reset=1", async () => {
    window.history.pushState({}, "", "/dashboard?reset=1");
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("reset-banner")).toBeInTheDocument());
  });

  it("calls router.replace('/dashboard') after first paint to clear the param", async () => {
    window.history.pushState({}, "", "/dashboard?reset=1");
    render(<DashboardPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/dashboard"));
  });

  it("dismisses the banner on click", async () => {
    window.history.pushState({}, "", "/dashboard?reset=1");
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("reset-banner")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await waitFor(() => expect(screen.queryByTestId("reset-banner")).toBeNull());
  });
});
