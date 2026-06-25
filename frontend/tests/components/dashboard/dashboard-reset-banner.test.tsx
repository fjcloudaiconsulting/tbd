import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import CustomDashboard from "@/components/dashboard/CustomDashboard";
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

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/dashboard/DashboardDataProvider", () => ({
  DashboardDataProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  useDashboard: vi.fn(() => ({
    refreshError: false,
    onDismissRefreshError: vi.fn(),
    refresh: vi.fn(),
  })),
}));

vi.mock("@/components/dashboard/DashboardPeriodNav", () => ({
  default: () => <div data-testid="period-nav" />,
}));

vi.mock("@/components/reports/Canvas", () => ({
  default: () => <div data-testid="canvas" />,
}));

vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
  getDefaultDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
  saveDashboard: vi.fn().mockResolvedValue({ layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} }),
}));

vi.mock("@/lib/reports/use-filter-chip-state", () => ({
  useFilterChipState: vi.fn(() => ({ accounts: [] })),
}));

const replaceMock = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/dashboard",
  useSearchParams: () => mockSearchParams,
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

describe("CustomDashboard — reset banner", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    replaceMock.mockReset();
    mockSearchParams = new URLSearchParams();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      features: { customDashboard: true } as never,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("does not render the banner without ?reset=1", async () => {
    render(<CustomDashboard />);
    await waitFor(() => expect(screen.queryByTestId("reset-banner")).toBeNull());
  });

  it("renders the banner when ?reset=1", async () => {
    mockSearchParams = new URLSearchParams("reset=1");
    render(<CustomDashboard />);
    await waitFor(() => expect(screen.getByTestId("reset-banner")).toBeInTheDocument());
  });

  it("calls router.replace('/dashboard') after first paint to clear the param", async () => {
    mockSearchParams = new URLSearchParams("reset=1");
    render(<CustomDashboard />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/dashboard"));
  });

  it("dismisses the banner on click", async () => {
    mockSearchParams = new URLSearchParams("reset=1");
    render(<CustomDashboard />);
    await waitFor(() => expect(screen.getByTestId("reset-banner")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await waitFor(() => expect(screen.queryByTestId("reset-banner")).toBeNull());
  });
});
