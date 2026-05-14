import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SettingsProfilePage from "@/app/settings/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import * as nextNavigation from "next/navigation";

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/settings",
  // Default: no `?sso_stepup_error=` in the URL. Tests that exercise
  // the banner can override this per-render via vi.mocked().
  useSearchParams: () => new URLSearchParams(),
}));

function makeUser(passwordSet: boolean) {
  return {
    id: 1,
    username: "alice",
    email: "alice@acme.io",
    first_name: null,
    last_name: null,
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "owner" as const,
    org_id: 1,
    org_name: "Acme",
    billing_cycle_day: 1,
    is_superadmin: false,
    is_active: true,
    mfa_enabled: false,
    password_set: passwordSet,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function mockUser(passwordSet: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(passwordSet) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

describe("Settings page — email change step-up token state hygiene", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  afterEach(() => {
    // Reset URL hash so cross-test bleed cannot drop a token into the
    // next render's mount effect.
    window.location.hash = "";
  });

  it("clears stepupToken on a step-up error so the UI stops claiming 'Google verified' (Finding 3)", async () => {
    mockUser(false);
    // Seed the URL fragment so the page picks up the token on mount.
    window.location.hash = "#stepup_token=stale-token";

    render(<SettingsProfilePage />);

    // The verified panel only renders while the email is actually
    // changing. Edit the field first.
    fireEvent.change(screen.getByLabelText(/^Email$/i), {
      target: { value: "new@acme.io" },
    });
    await screen.findByText(/Google verified/i);

    // User saves; backend rejects with a step-up failure (token
    // expired / not on row).
    vi.mocked(apiFetch).mockRejectedValueOnce(
      new Error("Step-up verification with Google is required to change email"),
    );
    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));

    // After the rejection the verified pill must be gone. Otherwise
    // the UI lies to the user about an unusable token.
    await waitFor(() => {
      expect(screen.queryByText(/Google verified/i)).not.toBeInTheDocument();
    });
    // And the Verify-with-Google button is back.
    expect(
      screen.getByRole("button", { name: /Verify with Google/i }),
    ).toBeInTheDocument();
  });

  it("renders the SSO step-up error banner when ?sso_stepup_error=state is on the URL", () => {
    mockUser(false);
    vi.spyOn(nextNavigation, "useSearchParams").mockReturnValue(
      new URLSearchParams("sso_stepup_error=state") as never,
    );
    render(<SettingsProfilePage />);
    const banner = screen.getByTestId("sso-stepup-error-banner");
    // Copy specific to the email-change-confirmation context.
    expect(banner.textContent).toMatch(/expired\. Try again to change your email/i);
    // And the retry CTA is wired to the existing Verify-with-Google flow.
    expect(
      screen.getByRole("button", { name: /Try again with Google/i }),
    ).toBeInTheDocument();
  });

  it("preserves stepupToken when the error is unrelated to step-up (e.g., email taken)", async () => {
    mockUser(false);
    window.location.hash = "#stepup_token=fresh-token";

    render(<SettingsProfilePage />);
    fireEvent.change(screen.getByLabelText(/^Email$/i), {
      target: { value: "taken@acme.io" },
    });
    await screen.findByText(/Google verified/i);

    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("Email already taken"));
    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));

    // After my backend reorder fix the duplicate-email check runs
    // BEFORE the token is consumed, so the token survives a 409 and
    // the UI correctly keeps "Google verified" so the retry is one
    // click away. Pinning that frontend keeps the token in this case.
    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalled();
    });
    expect(screen.getByText(/Google verified/i)).toBeInTheDocument();
  });
});
