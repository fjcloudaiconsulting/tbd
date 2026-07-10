import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import LoginPageBody from "@/components/auth/LoginPageBody";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return { ...actual, useAuth: vi.fn() };
});

// Stable spies so we can assert the exact post-login / bounce target.
const pushMock = vi.fn();
const replaceMock = vi.fn();
const searchParamsMock = vi.fn(() => new URLSearchParams());

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  useSearchParams: () => searchParamsMock(),
}));

describe("LoginPageBody — returnTo honoring", () => {
  const useAuthMock = vi.mocked(useAuth);
  const loginMock = vi.fn();

  function mockSignedOut() {
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: loginMock,
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  }

  beforeEach(() => {
    pushMock.mockReset();
    replaceMock.mockReset();
    searchParamsMock.mockReset();
    loginMock.mockReset();
    loginMock.mockResolvedValue(undefined);
    searchParamsMock.mockReturnValue(new URLSearchParams());
    mockSignedOut();
  });

  async function submitLogin() {
    fireEvent.change(screen.getByLabelText(/Email or Username/i), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "S3cret-Pass!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign In" }));
  }

  it("navigates to a safe returnTo after a successful login", async () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("returnTo=/transactions"));
    render(<LoginPageBody />);

    await submitLogin();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/transactions"));
  });

  it("falls back to /dashboard for a malicious returnTo after login", async () => {
    searchParamsMock.mockReturnValue(
      new URLSearchParams("returnTo=https://evil.com"),
    );
    render(<LoginPageBody />);

    await submitLogin();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
    expect(pushMock).not.toHaveBeenCalledWith("https://evil.com");
  });

  it("navigates to /dashboard after login when no returnTo is present", async () => {
    searchParamsMock.mockReturnValue(new URLSearchParams());
    render(<LoginPageBody />);

    await submitLogin();

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/dashboard"));
  });

  it("already-authenticated bounce honors a safe returnTo", async () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("returnTo=/accounts"));
    useAuthMock.mockReturnValue({
      user: { id: 1, username: "alice" },
      loading: false,
      needsSetup: false,
      login: loginMock,
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<LoginPageBody />);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/accounts"));
  });

  it("already-authenticated bounce falls back to /dashboard for a malicious returnTo", async () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("returnTo=//evil.com"));
    useAuthMock.mockReturnValue({
      user: { id: 1, username: "alice" },
      loading: false,
      needsSetup: false,
      login: loginMock,
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);

    render(<LoginPageBody />);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/dashboard"));
  });
});

describe("LoginPageBody — re-auth reason banner", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    searchParamsMock.mockReset();
    useAuthMock.mockReset();
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("shows the session-expired banner for reason=expired", () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("reason=expired"));
    render(<LoginPageBody />);
    const banner = screen.getByTestId("auth-reason-banner");
    expect(banner.textContent).toMatch(/session expired/i);
  });

  it("shows the signed-out banner for reason=logout", () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("reason=logout"));
    render(<LoginPageBody />);
    const banner = screen.getByTestId("auth-reason-banner");
    expect(banner.textContent).toMatch(/signed out/i);
  });

  it("shows no reason banner when reason is absent", () => {
    searchParamsMock.mockReturnValue(new URLSearchParams());
    render(<LoginPageBody />);
    expect(screen.queryByTestId("auth-reason-banner")).toBeNull();
  });

  it("shows no reason banner for an unknown reason value", () => {
    searchParamsMock.mockReturnValue(new URLSearchParams("reason=bogus"));
    render(<LoginPageBody />);
    expect(screen.queryByTestId("auth-reason-banner")).toBeNull();
  });
});
