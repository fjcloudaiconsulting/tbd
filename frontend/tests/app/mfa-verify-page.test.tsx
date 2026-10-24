/**
 * FE-03. The Google SSO MFA redirect lands on /mfa-verify?mfa_token=... The
 * page keeps the token and drops it from the address bar on mount; the
 * password-login path (token in sessionStorage) is unchanged.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import MfaVerifyPage from "@/app/mfa-verify/page";

const apiFetchMock = vi.fn();
const replaceMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  signInWithAccessToken: vi.fn(),
}));
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ user: null, loading: false }),
}));
// Reads the live URL, as Next.js does: once the page strips ?mfa_token= from
// the address bar, useSearchParams no longer returns it.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

beforeEach(() => {
  apiFetchMock.mockReset();
  replaceMock.mockReset();
  sessionStorage.clear();
});

describe("mfa-verify page, token in the URL (FE-03)", () => {
  it("drops mfa_token from the address bar and still sends it", async () => {
    window.history.replaceState(null, "", "/mfa-verify?mfa_token=sso-mfa-tok");
    render(<MfaVerifyPage />);

    const field = await screen.findByLabelText("Verification Code");
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(window.location.pathname).toBe("/mfa-verify");
    expect(replaceMock).not.toHaveBeenCalledWith("/login");

    // Fail the verify so the test does not navigate jsdom away.
    apiFetchMock.mockRejectedValueOnce(new Error("Invalid code"));
    fireEvent.change(field, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByText("Invalid code");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/mfa/verify",
      expect.objectContaining({
        body: JSON.stringify({ mfa_token: "sso-mfa-tok", code: "123456" }),
      }),
    );
  });

  it("still prefers the sessionStorage token from a password login", async () => {
    sessionStorage.setItem("mfa_token", "pw-mfa-tok");
    window.history.replaceState(null, "", "/mfa-verify");
    render(<MfaVerifyPage />);

    const field = await screen.findByLabelText("Verification Code");
    apiFetchMock.mockRejectedValueOnce(new Error("Invalid code"));
    fireEvent.change(field, { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByText("Invalid code");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/mfa/verify",
      expect.objectContaining({
        body: JSON.stringify({ mfa_token: "pw-mfa-tok", code: "654321" }),
      }),
    );
  });

  it("sends a visitor with no token to /login", async () => {
    window.history.replaceState(null, "", "/mfa-verify");
    render(<MfaVerifyPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
  });
});
