import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import LoginPageBody from "@/components/auth/LoginPageBody";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
  }),
}));


describe("LoginPageBody — email-not-verified flow", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);
  const loginMock = vi.fn();

  beforeEach(() => {
    apiFetchMock.mockReset();
    loginMock.mockReset();
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: loginMock,
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("snapshots the submitted login so resend uses the value at error time, not the edited value", async () => {
    loginMock.mockRejectedValue(
      new ApiResponseError(
        403,
        "Please verify your email to sign in.",
        "email_not_verified",
        { code: "email_not_verified", message: "Please verify your email to sign in." },
      ),
    );
    apiFetchMock.mockResolvedValue({ detail: "ok" } as never);

    render(<LoginPageBody />);

    const idInput = screen.getByLabelText(/Email or Username/i) as HTMLInputElement;
    const pwInput = screen.getByLabelText(/Password/i) as HTMLInputElement;

    fireEvent.change(idInput, { target: { value: "alice" } });
    fireEvent.change(pwInput, { target: { value: "S3cret-Pass!" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

    const resendBtn = await screen.findByRole("button", {
      name: /Resend verification email/i,
    });

    // User edits the input *after* the error fires.
    fireEvent.change(idInput, { target: { value: "bob" } });

    fireEvent.click(resendBtn);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/auth/resend-verification-public",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ login: "alice" }),
        }),
      );
    });
  });

  it("does not show the resend button for non-email-verification errors", async () => {
    loginMock.mockRejectedValue(
      new ApiResponseError(401, "Invalid credentials"),
    );

    render(<LoginPageBody />);

    fireEvent.change(screen.getByLabelText(/Email or Username/i), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText(/Password/i), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

    await screen.findByText(/Invalid credentials/i);

    expect(
      screen.queryByRole("button", { name: /Resend verification email/i }),
    ).toBeNull();
  });
});
