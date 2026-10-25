/**
 * FE-03 / TBD-654. The MFA challenge reaches /mfa-verify only through
 * sessionStorage (password login, or the Google callback page reading the URL
 * fragment). A ?mfa_token= in the address is dropped from the address bar and
 * never used.
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

describe("mfa-verify page, challenge token (FE-03, TBD-654)", () => {
  it("never uses a token from the query string, and drops it from the address bar", async () => {
    window.history.replaceState(null, "", "/mfa-verify?mfa_token=planted-tok");
    render(<MfaVerifyPage />);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/login"));
    expect(window.location.search).toBe("");
    expect(apiFetchMock).not.toHaveBeenCalled();
  });

  it("uses the sessionStorage token even when the URL carries another", async () => {
    sessionStorage.setItem("mfa_token", "pw-mfa-tok");
    window.history.replaceState(null, "", "/mfa-verify?mfa_token=planted-tok");
    render(<MfaVerifyPage />);

    const field = await screen.findByLabelText("Verification Code");
    await waitFor(() => expect(window.location.search).toBe(""));
    apiFetchMock.mockRejectedValueOnce(new Error("Invalid code"));
    fireEvent.change(field, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByText("Invalid code");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/mfa/verify",
      expect.objectContaining({
        body: JSON.stringify({ mfa_token: "pw-mfa-tok", code: "123456" }),
      }),
    );
  });

  it("uses the sessionStorage token from a password login", async () => {
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
