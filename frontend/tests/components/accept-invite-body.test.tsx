import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import AcceptInviteBody from "@/components/auth/AcceptInviteBody";
import { ApiResponseError, apiFetch, setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn(), setAccessToken: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return { ...actual, useAuth: vi.fn() };
});

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams("token=valid-token"),
}));


describe("AcceptInviteBody", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const setAccessTokenMock = vi.mocked(setAccessToken);
  const useAuthMock = vi.mocked(useAuth);
  const refreshMeMock = vi.fn();

  beforeEach(() => {
    apiFetchMock.mockReset();
    setAccessTokenMock.mockReset();
    refreshMeMock.mockReset();
    pushMock.mockReset();
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: refreshMeMock,
    });
  });

  it("loads the preview, submits the accept request, and routes to /dashboard", async () => {
    apiFetchMock
      .mockResolvedValueOnce({
        org_name: "Acme",
        email: "newbie@acme.io",
        role: "member",
        is_reactivation: false,
      } as never)
      .mockResolvedValueOnce({ access_token: "fresh-jwt" } as never);

    render(<AcceptInviteBody />);

    await screen.findByText(/Acme/);
    fireEvent.change(screen.getByLabelText(/Username/i), {
      target: { value: "newbie" },
    });
    fireEvent.change(screen.getByLabelText(/Password/i), {
      target: { value: "strong-pw-1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Accept/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenLastCalledWith(
        "/api/v1/orgs/invitations/accept",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            token: "valid-token",
            username: "newbie",
            password: "strong-pw-1234",
          }),
        }),
      );
    });
    await waitFor(() => {
      expect(setAccessTokenMock).toHaveBeenCalledWith("fresh-jwt");
      expect(pushMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("shows the unavailable message when preview returns 410", async () => {
    apiFetchMock.mockRejectedValueOnce(
      new ApiResponseError(
        410, "This invitation is no longer available.",
        "invitation_unavailable",
      ),
    );
    render(<AcceptInviteBody />);
    await screen.findByText(/no longer available/i);
    expect(
      screen.queryByRole("button", { name: /Accept/i }),
    ).toBeNull();
  });

  it("locks the username field and uses the existing username on reactivation", async () => {
    apiFetchMock
      .mockResolvedValueOnce({
        org_name: "Acme",
        email: "rejoiner@acme.io",
        role: "admin",
        is_reactivation: true,
        existing_username: "rejoiner",
      } as never)
      .mockResolvedValueOnce({ access_token: "tok" } as never);

    render(<AcceptInviteBody />);

    const usernameInput = await screen.findByLabelText<HTMLInputElement>(/Username/i);
    expect(usernameInput.value).toBe("rejoiner");
    expect(usernameInput.readOnly).toBe(true);

    fireEvent.change(screen.getByLabelText(/New password/i), {
      target: { value: "brand-new-pw-1234" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Rejoin/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenLastCalledWith(
        "/api/v1/orgs/invitations/accept",
        expect.objectContaining({
          body: JSON.stringify({
            token: "valid-token",
            username: "rejoiner",
            password: "brand-new-pw-1234",
          }),
        }),
      );
    });
  });
});
