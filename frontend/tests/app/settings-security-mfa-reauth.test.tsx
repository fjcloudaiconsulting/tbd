/**
 * BE-32: MFA changes on the security page carry the proofs the backend now
 * requires.
 *
 * - Setup re-authenticates first: the password, or for an account created
 *   with Google a step-up proof for the `mfa_setup` action. The proof comes
 *   back in `#stepup_token=` like the password-set one, so the page records
 *   which flow it started in sessionStorage.
 * - Disable and regenerate send a current authenticator or recovery code
 *   as `mfa_code`, beside the password.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SecurityPage from "@/app/settings/security/page";
import { apiFetch, ApiResponseError } from "@/lib/api";
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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/settings/security",
  useSearchParams: () => new URLSearchParams(),
}));

const PENDING_KEY = "tbd-stepup-pending-action";

function mockUser({ mfaEnabled, passwordSet }: { mfaEnabled: boolean; passwordSet: boolean }) {
  vi.mocked(useAuth).mockReturnValue({
    user: {
      id: 1,
      username: "alice",
      email: "alice@acme.io",
      first_name: null,
      last_name: null,
      phone: null,
      avatar_url: null,
      email_verified: true,
      role: "owner",
      org_id: 1,
      org_name: "Acme",
      billing_cycle_day: 1,
      is_superadmin: false,
      is_active: true,
      mfa_enabled: mfaEnabled,
      password_set: passwordSet,
      subscription_status: null,
      subscription_plan: null,
      trial_end: null,
    } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

function bodyOf(url: string): Record<string, unknown> | undefined {
  const call = vi.mocked(apiFetch).mock.calls.find(([u]) => u === url);
  const init = call?.[1] as { body?: string } | undefined;
  return init?.body ? JSON.parse(init.body) : undefined;
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/auth/mfa/setup") {
      return Promise.resolve({ qr_code: "Zm9v", secret: "SECRET" });
    }
    if (url === "/api/v1/auth/mfa/recovery-codes") {
      return Promise.resolve({ recovery_codes: ["a1b2-c3d4-e5f6-a7b8"] });
    }
    return Promise.resolve([]);
  }) as never);
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/settings/security");
});

describe("Security page: re-authentication before MFA setup (BE-32)", () => {
  it("asks for the password and sends it with the setup request", async () => {
    mockUser({ mfaEnabled: false, passwordSet: true });
    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
    );
    const start = screen.getByRole("button", { name: /^Start setup$/i });
    expect(start).toBeDisabled();
    expect(apiFetch).not.toHaveBeenCalledWith("/api/v1/auth/mfa/setup", expect.anything());

    fireEvent.change(screen.getByLabelText(/^Account password$/i), {
      target: { value: "hunter2" },
    });
    fireEvent.click(start);

    expect(await screen.findByText(/Scan this QR code/i)).toBeInTheDocument();
    expect(bodyOf("/api/v1/auth/mfa/setup")).toEqual({ current_password: "hunter2" });
  });

  it("maps a wrong password to a friendly message", async () => {
    mockUser({ mfaEnabled: false, passwordSet: true });
    vi.mocked(apiFetch).mockImplementation(((url: string) =>
      url === "/api/v1/auth/mfa/setup"
        ? Promise.reject(new ApiResponseError(400, "Current password is incorrect"))
        : Promise.resolve([])) as never);
    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
    );
    fireEvent.change(screen.getByLabelText(/^Account password$/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /^Start setup$/i }));
    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/password did not match/i);
  });

  it("starts a Google step-up for mfa_setup on an account without a password", async () => {
    mockUser({ mfaEnabled: false, passwordSet: false });
    const original = window.location;
    const hrefSetter = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...original,
        get href() {
          return original.href;
        },
        set href(value: string) {
          hrefSetter(value);
        },
      },
    });
    vi.mocked(apiFetch).mockImplementation(((url: string) =>
      url === "/api/v1/auth/sso-stepup/initiate"
        ? Promise.resolve({ redirect_url: "https://accounts.google.com/fake" })
        : Promise.resolve([])) as never);
    try {
      render(<SecurityPage />);
      fireEvent.click(
        await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
      );
      expect(screen.queryByLabelText(/^Account password$/i)).toBeNull();
      expect(screen.getByRole("button", { name: /^Start setup$/i })).toBeDisabled();
      // The MFA card's own Verify button (the password card has another).
      const verifyButtons = screen.getAllByRole("button", { name: /^Verify with Google$/i });
      fireEvent.click(verifyButtons[verifyButtons.length - 1]);

      await waitFor(() => expect(hrefSetter).toHaveBeenCalledWith("https://accounts.google.com/fake"));
      expect(bodyOf("/api/v1/auth/sso-stepup/initiate")).toEqual({ action: "mfa_setup" });
      expect(window.sessionStorage.getItem(PENDING_KEY)).toBe("mfa_setup");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: original });
    }
  });

  it("routes a returning mfa_setup proof to the setup form, not the password form", async () => {
    mockUser({ mfaEnabled: false, passwordSet: false });
    window.sessionStorage.setItem(PENDING_KEY, "mfa_setup");
    window.history.replaceState(null, "", "/settings/security#stepup_token=proof-123");

    render(<SecurityPage />);
    expect(await screen.findByText(/Google verified\. Start setup to continue\./i)).toBeInTheDocument();
    expect(screen.queryByText(/Google verified\. Enter your new password/i)).toBeNull();
    expect(window.location.hash).toBe("");
    expect(window.sessionStorage.getItem(PENDING_KEY)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Start setup$/i }));
    expect(await screen.findByText(/Scan this QR code/i)).toBeInTheDocument();
    expect(bodyOf("/api/v1/auth/mfa/setup")).toEqual({ stepup_token: "proof-123" });
  });

  it("keeps a proof with no recorded flow on the password form, as before", async () => {
    mockUser({ mfaEnabled: false, passwordSet: false });
    window.history.replaceState(null, "", "/settings/security#stepup_token=proof-456");

    render(<SecurityPage />);
    expect(await screen.findByText(/Google verified\. Enter your new password/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Start setup$/i })).toBeNull();
  });
});

describe("Security page: a second factor to disable or regenerate (BE-32)", () => {
  it("sends the code with the password when disabling", async () => {
    mockUser({ mfaEnabled: true, passwordSet: true });
    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Disable two-factor authentication/i }),
    );
    fireEvent.change(screen.getByLabelText(/^Password$/i), { target: { value: "hunter2" } });
    const submit = screen.getByRole("button", { name: /^Disable two-factor$/i });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Authenticator or recovery code/i), {
      target: { value: " 123456 " },
    });
    fireEvent.click(submit);
    await waitFor(() => expect(bodyOf("/api/v1/auth/mfa/disable")).toBeDefined());
    expect(bodyOf("/api/v1/auth/mfa/disable")).toEqual({ password: "hunter2", mfa_code: "123456" });
  });

  it("maps a wrong code to its own message", async () => {
    mockUser({ mfaEnabled: true, passwordSet: true });
    vi.mocked(apiFetch).mockImplementation(((url: string) =>
      url === "/api/v1/auth/mfa/disable"
        ? Promise.reject(new ApiResponseError(403, "Invalid authentication code"))
        : Promise.resolve([])) as never);
    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Disable two-factor authentication/i }),
    );
    fireEvent.change(screen.getByLabelText(/^Password$/i), { target: { value: "hunter2" } });
    fireEvent.change(screen.getByLabelText(/Authenticator or recovery code/i), {
      target: { value: "000000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Disable two-factor$/i }));
    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/code did not match/i);
    expect(err.textContent).not.toMatch(/password/i);
  });

  it("sends the code with the password when regenerating recovery codes", async () => {
    mockUser({ mfaEnabled: true, passwordSet: true });
    render(<SecurityPage />);
    fireEvent.click(await screen.findByRole("button", { name: /Regenerate recovery codes/i }));
    fireEvent.change(screen.getByLabelText(/Confirm password/i), { target: { value: "hunter2" } });
    fireEvent.change(screen.getByLabelText(/Authenticator or recovery code/i), {
      target: { value: "a1b2-c3d4-e5f6-a7b8" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Regenerate$/i }));
    expect(await screen.findByText(/New recovery codes/i)).toBeInTheDocument();
    expect(bodyOf("/api/v1/auth/mfa/recovery-codes")).toEqual({
      password: "hunter2",
      mfa_code: "a1b2-c3d4-e5f6-a7b8",
    });
  });
});
