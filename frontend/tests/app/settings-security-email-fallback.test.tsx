/**
 * BE-03: the email-code sign-in fallback is a toggle on the security page.
 *
 * - It shows only for an account with two-factor on, says whether it is on,
 *   and explains the trade-off.
 * - Turning it on asks for a current authenticator or recovery code and sends
 *   it as `mfa_code`; turning it off sends no code.
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

const FALLBACK_URL = "/api/v1/auth/mfa/email-fallback";
const refreshMe = vi.fn();

function mockUser({ mfaEnabled, fallback }: { mfaEnabled: boolean; fallback?: boolean }) {
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
      mfa_email_fallback: fallback,
      password_set: true,
      subscription_status: null,
      subscription_plan: null,
      trial_end: null,
    } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe,
  } as never);
}

function fallbackCalls() {
  return vi.mocked(apiFetch).mock.calls.filter(([u]) => u === FALLBACK_URL);
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse((call[1] as { body: string }).body);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(((url: string) =>
    url === FALLBACK_URL ? Promise.resolve({ mfa_email_fallback: true }) : Promise.resolve([])) as never);
  refreshMe.mockReset();
  refreshMe.mockResolvedValue(undefined);
});

describe("Security page: email-code fallback (BE-03)", () => {
  it("is hidden while two-factor is off", () => {
    mockUser({ mfaEnabled: false });
    render(<SecurityPage />);
    expect(screen.queryByRole("heading", { name: /^Email fallback$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /email codes/i })).toBeNull();
  });

  it("shows Off and the trade-off for a two-factor account that never opted in", () => {
    mockUser({ mfaEnabled: true });
    render(<SecurityPage />);
    expect(screen.getByRole("heading", { name: /^Email fallback$/i })).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(screen.getByText(/could then sign in without your authenticator/i)).toBeInTheDocument();
    expect(screen.getByText("alice@acme.io")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Turn on email codes$/i })).toBeInTheDocument();
  });

  it("asks for a code and sends it to turn the fallback on", async () => {
    mockUser({ mfaEnabled: true, fallback: false });
    render(<SecurityPage />);
    fireEvent.click(screen.getByRole("button", { name: /^Turn on email codes$/i }));

    const submit = screen.getByRole("button", { name: /^Turn on$/i });
    expect(submit).toBeDisabled();
    expect(fallbackCalls()).toHaveLength(0);

    fireEvent.change(screen.getByLabelText(/^Authenticator or recovery code$/i), {
      target: { value: " 123456 " },
    });
    fireEvent.click(submit);

    expect(await screen.findByText("Email codes are on.")).toBeInTheDocument();
    const calls = fallbackCalls();
    expect(calls).toHaveLength(1);
    expect((calls[0][1] as { method: string }).method).toBe("PUT");
    expect(bodyOf(calls[0])).toEqual({ enabled: true, mfa_code: "123456" });
    await waitFor(() => expect(refreshMe).toHaveBeenCalled());
  });

  it("maps a wrong code to a friendly message and keeps the form open", async () => {
    mockUser({ mfaEnabled: true, fallback: false });
    vi.mocked(apiFetch).mockImplementation(((url: string) =>
      url === FALLBACK_URL
        ? Promise.reject(new ApiResponseError(403, "Invalid authentication code"))
        : Promise.resolve([])) as never);
    render(<SecurityPage />);
    fireEvent.click(screen.getByRole("button", { name: /^Turn on email codes$/i }));
    fireEvent.change(screen.getByLabelText(/^Authenticator or recovery code$/i), {
      target: { value: "000000" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Turn on$/i }));

    const err = await screen.findByRole("alert");
    expect(err.textContent).toMatch(/code did not match/i);
    expect(screen.getByLabelText(/^Authenticator or recovery code$/i)).toHaveValue("000000");
    expect(refreshMe).not.toHaveBeenCalled();
  });

  it("turns the fallback off without asking for a code", async () => {
    mockUser({ mfaEnabled: true, fallback: true });
    render(<SecurityPage />);
    expect(screen.getByText("On")).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Authenticator or recovery code$/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /^Turn off email codes$/i }));

    expect(await screen.findByText("Email codes are off.")).toBeInTheDocument();
    const calls = fallbackCalls();
    expect(calls).toHaveLength(1);
    expect(bodyOf(calls[0])).toEqual({ enabled: false });
    await waitFor(() => expect(refreshMe).toHaveBeenCalled());
  });
});
