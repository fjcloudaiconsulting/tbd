import { render, screen, waitFor } from "@testing-library/react";

import SystemHubPage from "@/app/system/page";
import { useAuth } from "@/components/auth/AuthProvider";

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
  usePathname: () => "/system",
}));

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true, mfa_enabled: false,
  subscription_status: null, subscription_plan: null, trial_end: null,
};

const PLAIN_USER = { ...SUPERADMIN, id: 2, is_superadmin: false };
const ADMIN_VIEW_ONLY = { ...PLAIN_USER, id: 3, permissions: ["admin.view"] };
const ADMIN_VIEW_AND_PLANS = {
  ...PLAIN_USER,
  id: 4,
  permissions: ["admin.view", "plans.manage"],
};

describe("SystemHubPage (L5.8)", () => {
  beforeEach(() => {
    replaceMock.mockReset();
  });

  it("renders subsection cards for superadmin", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<SystemHubPage />);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: /system/i })).toBeInTheDocument(),
    );
    // Plans card present and linked. The shell sidebar also has links
    // matching /plans/ ("Forecast plans"), so locate the hub card by
    // its exact href instead of by a regex on accessible name.
    const allLinks = screen.getAllByRole("link");
    const plansLink = allLinks.find((a) => a.getAttribute("href") === "/system/plans");
    expect(plansLink).toBeDefined();
    // Card-level heading ("Plan Catalog") is reachable inside the link
    // element. Match the substring case-insensitively so the assertion
    // still passes if the hub card title changes shape (e.g. "Plan
    // Catalog" vs "Subscription Plan Catalog") as long as it keeps the
    // word "plan".
    expect(plansLink!.textContent).toMatch(/plan/i);

    // Feature Flags card is also shown for superadmin.
    const featuresLink = allLinks.find((a) => a.getAttribute("href") === "/system/features");
    expect(featuresLink).toBeDefined();
    expect(featuresLink!.textContent).toMatch(/feature/i);

    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("redirects non-superadmin without admin.view to /dashboard and renders nothing", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: PLAIN_USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    const { container } = render(<SystemHubPage />);
    expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    // Component returns null while gated; no headings or cards rendered.
    expect(container).toBeEmptyDOMElement();
  });

  it("hides cards whose destination the user can't open (admin.view only)", async () => {
    // User holds admin.view (so hub renders) but not plans.manage. The
    // Plans card should be filtered out, leaving an empty-state line.
    vi.mocked(useAuth).mockReturnValue({
      user: ADMIN_VIEW_ONLY as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<SystemHubPage />);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: /system/i })).toBeInTheDocument(),
    );
    expect(replaceMock).not.toHaveBeenCalled();
    // No Plans card link present.
    const allLinks = screen.getAllByRole("link");
    expect(allLinks.find((a) => a.getAttribute("href") === "/system/plans")).toBeUndefined();
    // Empty-state message renders.
    expect(
      screen.getByText(/don.?t have access to any platform-admin surfaces/i),
    ).toBeInTheDocument();
  });

  it("hides the Feature Flags card for non-superadmin (superadminOnly guard)", async () => {
    // A user with admin.view + plans.manage but not is_superadmin should
    // see the Plans card but NOT the Feature Flags card (superadminOnly).
    vi.mocked(useAuth).mockReturnValue({
      user: ADMIN_VIEW_AND_PLANS as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<SystemHubPage />);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: /system/i })).toBeInTheDocument(),
    );
    const allLinks = screen.getAllByRole("link");
    const featuresLink = allLinks.find((a) => a.getAttribute("href") === "/system/features");
    expect(featuresLink).toBeUndefined();
    // Plans card is still present.
    const plansLink = allLinks.find((a) => a.getAttribute("href") === "/system/plans");
    expect(plansLink).toBeDefined();
  });

  it("renders Plans card for non-superadmin with admin.view + plans.manage", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: ADMIN_VIEW_AND_PLANS as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<SystemHubPage />);
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: /system/i })).toBeInTheDocument(),
    );
    expect(replaceMock).not.toHaveBeenCalled();
    const allLinks = screen.getAllByRole("link");
    const plansLink = allLinks.find((a) => a.getAttribute("href") === "/system/plans");
    expect(plansLink).toBeDefined();
  });

  it("renders nothing while auth is still loading", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null as never,
      loading: true,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    const { container } = render(<SystemHubPage />);
    expect(replaceMock).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("redirects unauthenticated visitors to /login (auth settled, user=null)", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    const { container } = render(<SystemHubPage />);
    expect(replaceMock).toHaveBeenCalledWith("/login");
    // Component returns null while gated; no headings or cards rendered.
    expect(container).toBeEmptyDOMElement();
  });
});
