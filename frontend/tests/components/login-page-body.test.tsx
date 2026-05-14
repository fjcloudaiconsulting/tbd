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

// Per-test override target so individual cases can supply their own
// `?sso_error=<code>` payload without leaking state. Default returns
// no query params.
const searchParamsMock = vi.fn(() => new URLSearchParams());

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
  }),
  useSearchParams: () => searchParamsMock(),
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
    const pwInput = screen.getByLabelText("Password") as HTMLInputElement;

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

  it("shows the login the resend action targets, even if the user edits the input", async () => {
    loginMock.mockRejectedValue(
      new ApiResponseError(
        403,
        "Please verify your email to sign in.",
        "email_not_verified",
        { code: "email_not_verified", message: "Please verify your email to sign in." },
      ),
    );

    render(<LoginPageBody />);

    const idInput = screen.getByLabelText(/Email or Username/i) as HTMLInputElement;
    const pwInput = screen.getByLabelText("Password") as HTMLInputElement;

    fireEvent.change(idInput, { target: { value: "alice" } });
    fireEvent.change(pwInput, { target: { value: "S3cret-Pass!" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

    await screen.findByRole("button", { name: /Resend verification email/i });

    // The targeted login is rendered visibly next to the resend button.
    expect(screen.getByText("alice")).toBeTruthy();

    // User edits the input after the error fires; the displayed target stays "alice".
    fireEvent.change(idInput, { target: { value: "bob" } });
    expect(screen.getByText("alice")).toBeTruthy();
    expect(screen.queryByText("bob")).toBeNull();
  });

  it("does not surface the SSO error banner when the URL has no sso_error param", () => {
    searchParamsMock.mockReturnValue(new URLSearchParams());
    render(<LoginPageBody />);
    expect(screen.queryByTestId("sso-error-banner")).toBeNull();
  });

  it("does not show the resend button for non-email-verification errors", async () => {
    loginMock.mockRejectedValue(
      new ApiResponseError(401, "Invalid credentials"),
    );

    render(<LoginPageBody />);

    fireEvent.change(screen.getByLabelText(/Email or Username/i), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));

    await screen.findByText(/Invalid credentials/i);

    expect(
      screen.queryByRole("button", { name: /Resend verification email/i }),
    ).toBeNull();
  });
});


describe("LoginPageBody — SSO error banner", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    searchParamsMock.mockReset();
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  // Every code the backend emits in `?sso_error=<code>` must map to
  // banner copy. Pin them all so a future backend code that ships
  // without a frontend update at least gets the generic fallback,
  // and so renaming a code in the backend forces a test update.
  const cases: Array<{ code: string; needle: RegExp }> = [
    { code: "state", needle: /expired\. Try again/i },
    { code: "token", needle: /sign-in failed\. Try again/i },
    { code: "userinfo", needle: /sign-in failed\. Try again/i },
    { code: "unverified", needle: /isn't verified/i },
    { code: "deactivated", needle: /account is deactivated/i },
    { code: "no_email", needle: /didn't return an email/i },
  ];

  it.each(cases)("renders friendly copy for ?sso_error=$code", ({ code, needle }) => {
    searchParamsMock.mockReturnValue(new URLSearchParams(`sso_error=${code}`));
    render(<LoginPageBody />);
    const banner = screen.getByTestId("sso-error-banner");
    expect(banner.textContent).toMatch(needle);
  });

  it("falls back to a generic message for an unknown sso_error code", () => {
    // Defensive: a future backend code that ships before the frontend
    // catches up still surfaces a clear retry message, not a blank
    // banner.
    searchParamsMock.mockReturnValue(new URLSearchParams("sso_error=brand_new_code"));
    render(<LoginPageBody />);
    const banner = screen.getByTestId("sso-error-banner");
    expect(banner.textContent).toMatch(/didn't complete\. Try again/i);
  });

  it("Try again button calls the existing /api/v1/auth/google flow", async () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("sso_error=state"));
    apiFetchMock.mockResolvedValue({
      redirect_url: "https://accounts.google.com/fake",
    } as never);

    render(<LoginPageBody />);
    const retryBtn = screen.getByRole("button", { name: /Try again with Google/i });
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/google");
    });
  });
});
