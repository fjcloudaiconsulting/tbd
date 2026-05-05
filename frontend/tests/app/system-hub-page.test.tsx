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
    // Card-level heading ("Plans") is reachable inside the link element.
    expect(plansLink!.textContent).toMatch(/plans/i);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("redirects non-superadmin to /dashboard and renders nothing", () => {
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
});
