/**
 * FE-03. The reset link carries a single-use token in `?token=`. The page
 * keeps it in state and drops it from the address bar on mount, so it does
 * not stay in history or in the Referer of later requests.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ResetPasswordPage from "@/app/reset-password/page";

const apiFetchMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));
// Reads the live URL, as Next.js does: once the page strips ?token= from the
// address bar, useSearchParams no longer returns it.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

beforeEach(() => {
  apiFetchMock.mockReset();
  window.history.replaceState(null, "", "/reset-password?token=reset-tok-123");
});

describe("reset-password page, token in the URL (FE-03)", () => {
  it("drops the token from the address bar and still posts it on submit", async () => {
    render(<ResetPasswordPage />);

    const field = await screen.findByLabelText("New Password");
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(window.location.pathname).toBe("/reset-password");

    apiFetchMock.mockResolvedValueOnce({ detail: "ok" });
    fireEvent.change(field, { target: { value: "new-pass-1234" } });
    fireEvent.change(screen.getByLabelText("Confirm Password"), {
      target: { value: "new-pass-1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset Password" }));

    await screen.findByText("Password reset successfully!");
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/reset-password",
      expect.objectContaining({
        body: JSON.stringify({ token: "reset-tok-123", new_password: "new-pass-1234" }),
      }),
    );
  });

  it("shows the invalid-link state when there is no token", async () => {
    window.history.replaceState(null, "", "/reset-password");
    render(<ResetPasswordPage />);
    expect(await screen.findByText("Invalid reset link.")).toBeTruthy();
  });
});
