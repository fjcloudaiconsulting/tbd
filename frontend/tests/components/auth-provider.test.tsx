import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, MfaRequiredError, useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, setAccessToken } from "@/lib/api";
import type { User } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
  setAccessToken: vi.fn(),
}));


const TEST_USER: User = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: "Tester",
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Test Org",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};


function Harness() {
  const { user, loading, needsSetup, login, logout } = useAuth();
  const [error, setError] = useState("none");

  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="needs-setup">{String(needsSetup)}</div>
      <div data-testid="user">{user?.email ?? "none"}</div>
      <div data-testid="error">{error}</div>
      <button
        onClick={() => {
          login("alice", "secret").catch((err) => {
            if (err instanceof Error) {
              setError(`${err.name}:${err.message}`);
              return;
            }
            setError(String(err));
          });
        }}
      >
        Login
      </button>
      <button
        onClick={() => {
          logout().catch(() => {});
        }}
      >
        Logout
      </button>
    </div>
  );
}


describe("AuthProvider", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const setAccessTokenMock = vi.mocked(setAccessToken);

  beforeEach(() => {
    apiFetchMock.mockReset();
    setAccessTokenMock.mockReset();
  });

  it("stops at setup mode without attempting session restore", async () => {
    apiFetchMock.mockResolvedValueOnce({ needs_setup: true });

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    expect(screen.getByTestId("needs-setup")).toHaveTextContent("true");
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/auth/status");
  });

  it("restores an existing session on mount", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    expect(setAccessTokenMock).toHaveBeenCalledWith("restored-token");
    expect(apiFetchMock).toHaveBeenNthCalledWith(2, "/api/v1/auth/refresh", {
      method: "POST",
    });
    expect(apiFetchMock).toHaveBeenNthCalledWith(3, "/api/v1/auth/me");
  });

  it("logs in interactively and loads the current user", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new Error("no session"))
      .mockResolvedValueOnce({ access_token: "login-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    fireEvent.click(screen.getByText("Login"));

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    expect(setAccessTokenMock).toHaveBeenCalledWith("login-token");
    expect(apiFetchMock).toHaveBeenNthCalledWith(3, "/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ login: "alice", password: "secret" }),
    });
  });

  it("surfaces MFA challenges to the caller", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new Error("no session"))
      .mockResolvedValueOnce({ mfa_required: true, mfa_token: "mfa-token" });

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    fireEvent.click(screen.getByText("Login"));

    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("MfaRequiredError:MFA required"),
    );

    const error = new MfaRequiredError("mfa-token");
    expect(error.mfaToken).toBe("mfa-token");
  });

  it("clears user state on logout even if the API call fails", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockResolvedValueOnce(TEST_USER)
      .mockRejectedValueOnce(new Error("network down"));

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    fireEvent.click(screen.getByText("Logout"));

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent("none"),
    );

    expect(setAccessTokenMock).toHaveBeenLastCalledWith(null);
  });

  it("clears user state when apiFetch dispatches auth:unauthenticated", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );

    act(() => {
      window.dispatchEvent(new Event("auth:unauthenticated"));
    });

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent("none"),
    );

    expect(setAccessTokenMock).toHaveBeenLastCalledWith(null);
  });
});
