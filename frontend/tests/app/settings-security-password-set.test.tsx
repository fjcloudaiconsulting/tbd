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
    // Helper copy explains *why* there is no current password field.
    expect(
      screen.getByText(/created with Google/i),
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

  it("submits new_password only (no current_password) when password_set is false", async () => {
    mockUser(false);
    render(<SecurityPage />);
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
          body: JSON.stringify({ new_password: "fresh-password-1" }),
        }),
      );
    });
  });
});
