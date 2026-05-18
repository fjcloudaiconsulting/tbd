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

function makeUser(mfaEnabled: boolean) {
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
    mfa_enabled: mfaEnabled,
    password_set: true,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function mockUser(mfaEnabled: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(mfaEnabled) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

describe("Security page MFA polish: copy, validation, error mapping", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(apiFetch).mockResolvedValue([] as never);
  });

  it("disables the Set Up button while in flight and shows a spinner", async () => {
    mockUser(false);
    // Hang the setup call so we can observe the busy state.
    let resolve: (v: unknown) => void = () => {};
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/setup") {
        return new Promise((r) => { resolve = r; });
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    const setupBtn = await screen.findByRole("button", {
      name: /Set Up Two-Factor Authentication/i,
    });
    fireEvent.click(setupBtn);

    const busyBtn = await screen.findByRole("button", { name: /Setting up/i });
    expect(busyBtn).toBeDisabled();
    expect(busyBtn).toHaveAttribute("aria-busy", "true");

    resolve({ qr_code: "Zm9v", secret: "S" });
    await waitFor(() => {
      expect(screen.getByText(/Scan this QR code/i)).toBeInTheDocument();
    });
  });

  it("maps a 401 TOTP error to a friendly message and keeps the user's code filled", async () => {
    mockUser(false);
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/setup") {
        return Promise.resolve({ qr_code: "Zm9v", secret: "SECRET" });
      }
      if (url === "/api/v1/auth/mfa/enable") {
        return Promise.reject(new ApiResponseError(401, "Invalid TOTP code"));
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    const setupBtn = await screen.findByRole("button", {
      name: /Set Up Two-Factor Authentication/i,
    });
    fireEvent.click(setupBtn);

    // Advance to the verify step.
    fireEvent.click(await screen.findByRole("button", { name: /^Continue$/i }));

    const codeInput = await screen.findByLabelText(/Verification Code/i);
    fireEvent.change(codeInput, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and Enable/i }));

    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/did not match/i);
    // Raw server detail must not leak through.
    expect(err.textContent).not.toMatch(/Invalid TOTP code/i);
    // The code stays filled so the admin can correct it.
    expect((codeInput as HTMLInputElement).value).toBe("123456");
  });

  it("renders the 6-digit hint and ties it to the input via aria-describedby", async () => {
    mockUser(false);
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/setup") {
        return Promise.resolve({ qr_code: "Zm9v", secret: "SECRET" });
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Continue$/i }));

    const codeInput = await screen.findByLabelText(/Verification Code/i);
    const describedBy = codeInput.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const hint = document.getElementById(describedBy!);
    expect(hint?.textContent).toMatch(/30 seconds/i);
  });

  it("recovery codes screen makes Done depend on the confirmation checkbox", async () => {
    mockUser(false);
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/setup") {
        return Promise.resolve({ qr_code: "Zm9v", secret: "SECRET" });
      }
      if (url === "/api/v1/auth/mfa/enable") {
        return Promise.resolve({ recovery_codes: ["A1B2C3D4", "E5F6G7H8"] });
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Continue$/i }));
    const codeInput = await screen.findByLabelText(/Verification Code/i);
    fireEvent.change(codeInput, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and Enable/i }));

    const done = await screen.findByRole("button", { name: /^Done$/i });
    expect(done).toBeDisabled();
    // Hint must be present and reachable from the disabled button.
    const describedBy = done.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/I have saved these recovery codes/i));
    expect(done).toBeEnabled();
  });

  it("regenerate flow keeps the password filled on error and maps the 401", async () => {
    mockUser(true);
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/recovery-codes") {
        return Promise.reject(new ApiResponseError(401, "Invalid password"));
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Regenerate recovery codes/i }),
    );
    const pwd = await screen.findByLabelText(/Confirm password/i);
    fireEvent.change(pwd, { target: { value: "wrongPassword" } });
    fireEvent.click(screen.getByRole("button", { name: /^Regenerate$/i }));

    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/password did not match/i);
    expect(err.textContent).not.toMatch(/Invalid password/);
    expect((pwd as HTMLInputElement).value).toBe("wrongPassword");
  });

  // ── 2026-05-18 P2 review fix: refreshMe() now rejects on /me failure ─────
  //
  // Pre-fix, MFA enable's Done button and MFA disable's submit both
  // fire-and-forgot refreshMe(). After fetchMe() was changed to rethrow
  // on transient / terminal /auth/me errors, those promises started
  // unhandled-rejecting. Beyond the console noise, the saved-but-stale
  // user object meant the page kept showing the pre-change MFA UI even
  // though the setting flipped server-side. These tests pin the "saved,
  // reload to refresh profile" hint the handlers now surface.

  it("MFA enable Done button surfaces a reload hint when refreshMe rejects", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser(false) as never,
      loading: false,
      needsSetup: false,
      login: vi.fn().mockResolvedValue(undefined),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn().mockRejectedValue(new ApiResponseError(503, "refresh_transient")),
    } as never);

    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/setup") {
        return Promise.resolve({ qr_code: "Zm9v", secret: "SECRET" });
      }
      if (url === "/api/v1/auth/mfa/enable") {
        return Promise.resolve({ recovery_codes: ["A1B2C3D4", "E5F6G7H8"] });
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Set Up Two-Factor Authentication/i }),
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Continue$/i }));
    const codeInput = await screen.findByLabelText(/Verification Code/i);
    fireEvent.change(codeInput, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify and Enable/i }));

    fireEvent.click(
      await screen.findByLabelText(/I have saved these recovery codes/i),
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Done$/i }));

    // The reload hint replaces the bare success text when refreshMe
    // rejects — the setting IS saved server-side, but the local user
    // object is stale until the user reloads.
    await waitFor(() => {
      expect(
        screen.getByText(/Reload the page to refresh your profile state/i),
      ).toBeInTheDocument();
    });
  });

  it("MFA disable surfaces a reload hint when refreshMe rejects", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser(true) as never,
      loading: false,
      needsSetup: false,
      login: vi.fn().mockResolvedValue(undefined),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn().mockRejectedValue(new ApiResponseError(503, "refresh_transient")),
    } as never);

    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/auth/mfa/disable") {
        return Promise.resolve({});
      }
      return Promise.resolve([]);
    }) as never);

    render(<SecurityPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Disable two-factor authentication/i }),
    );
    const pwd = await screen.findByLabelText(/^Password$/i);
    fireEvent.change(pwd, { target: { value: "correctPassword" } });
    fireEvent.click(screen.getByRole("button", { name: /^Disable two-factor$/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Reload the page to refresh your profile state/i),
      ).toBeInTheDocument();
    });
  });
});
