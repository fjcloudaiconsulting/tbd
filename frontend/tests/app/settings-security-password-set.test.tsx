import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SecurityPage from "@/app/settings/security/page";
import { apiFetch } from "@/lib/api";
import { useAuth, MfaRequiredError } from "@/components/auth/AuthProvider";

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
  usePathname: () => "/settings/security",
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

describe("Security page — Set a Password / Change Password gate", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    // Default: every read endpoint returns empty so the MFA/session
    // sub-cards mount cleanly without coupling to this test.
    vi.mocked(apiFetch).mockResolvedValue([] as never);
  });

  it("renders 'Set a Password' card when password_set is false", async () => {
    mockUser(false);
    render(<SecurityPage />);
    expect(
      await screen.findByRole("heading", { name: /Set a Password/i }),
    ).toBeInTheDocument();
    // The current-password input is intentionally hidden in this branch.
    expect(screen.queryByLabelText(/Current Password/i)).not.toBeInTheDocument();
    // Helper copy frames the step-up requirement (replaces the older
    // "created with Google" sentence after Finding 1 tightened this
    // path).
    expect(
      screen.getByText(/Verify with Google to set a password/i),
    ).toBeInTheDocument();
  });

  it("renders 'Change Password' card when password_set is true", async () => {
    mockUser(true);
    render(<SecurityPage />);
    expect(
      await screen.findByRole("heading", { name: /Change Password/i }),
    ).toBeInTheDocument();
    // Standard branch keeps the current-password input.
    expect(screen.getByLabelText(/Current Password/i)).toBeInTheDocument();
  });

  it("disables Set Password until the user verifies with Google", async () => {
    mockUser(false);
    render(<SecurityPage />);
    const submit = screen.getByRole("button", { name: /Set Password/i });
    expect(submit).toBeDisabled();
  });

  it("does not POST /me/password without a step-up token (Finding 1)", async () => {
    mockUser(false);
    render(<SecurityPage />);
    fireEvent.change(screen.getByLabelText(/^New Password$/i), {
      target: { value: "fresh-password-1" },
    });
    fireEvent.change(screen.getByLabelText(/Confirm New Password/i), {
      target: { value: "fresh-password-1" },
    });
    // The submit button is disabled until verification completes; the
    // critical invariant is that no request is fired.
    fireEvent.click(screen.getByRole("button", { name: /Set Password/i }));

    await waitFor(() => {
      // No password POST should have happened. (Other endpoints, like
      // the security page's settings reads, are gated to admins only
      // and stay quiet for this fixture.)
      const passwordCalls = vi.mocked(apiFetch).mock.calls.filter(
        ([url]) => url === "/api/v1/users/me/password",
      );
      expect(passwordCalls).toHaveLength(0);
    });
  });

  it("clicking 'Verify with Google' POSTs initiate with return_to=security", async () => {
    mockUser(false);
    vi.mocked(apiFetch).mockResolvedValueOnce({
      redirect_url: "https://accounts.google.com/o/oauth2/v2/auth?state=stepup",
    } as never);
    // Stub navigation so the test doesn't actually leave the page.
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      writable: true,
      value: { ...originalLocation, href: "" },
    });

    render(<SecurityPage />);
    fireEvent.click(screen.getByRole("button", { name: /Verify with Google/i }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/v1/auth/sso-stepup/initiate",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ return_to: "security" }),
        }),
      );
    });

    Object.defineProperty(window, "location", {
      writable: true,
      value: originalLocation,
    });
  });

  it("submits stepup_token with new_password when password_set is false", async () => {
    mockUser(false);
    // Seed the URL fragment so the page picks up the token on mount,
    // mirroring the redirect from the SSO step-up callback.
    const originalHash = window.location.hash;
    window.location.hash = "#stepup_token=abcdef-step-up-token";
    try {
      render(<SecurityPage />);
      // The mount-effect strips the hash and fills `stepupToken`.
      // Wait for the "Google verified" copy to appear.
      await screen.findByText(/Google verified/i);

      fireEvent.change(screen.getByLabelText(/^New Password$/i), {
        target: { value: "fresh-password-1" },
      });
      fireEvent.change(screen.getByLabelText(/Confirm New Password/i), {
        target: { value: "fresh-password-1" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Set Password/i }));

      await waitFor(() => {
        expect(apiFetch).toHaveBeenCalledWith(
          "/api/v1/users/me/password",
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({
              new_password: "fresh-password-1",
              stepup_token: "abcdef-step-up-token",
            }),
          }),
        );
      });
    } finally {
      window.location.hash = originalHash;
    }
  });
});
