import React, { useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, MfaRequiredError, useAuth } from "@/components/auth/AuthProvider";
import {
  ApiResponseError,
  ApiTimeoutError,
  apiFetch,
  setAccessToken,
} from "@/lib/api";
import type { User } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  // Keep the real error classes so the AuthProvider's instanceof
  // checks (terminal vs transient discrimination, 2026-05-18 restore-
  // retry fix) work in tests — but stub the network surface.
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
    setAccessToken: vi.fn(),
  };
});


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
  password_set: true,
  allow_manual_balance_adjustment: false,
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
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"))
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
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"))
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

  // ── 2026-05-18 session-stability: restore() transient-retry budget ──────

  it("retries /auth/refresh on transient timeout during restore", async () => {
    // /auth/status succeeds, /auth/refresh times out twice then succeeds
    // on the third attempt. The user must end up signed in — without
    // the retry budget the first timeout would land them on /login
    // even though their cookie was perfectly valid.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockResolvedValueOnce({ access_token: "recovered-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );
    expect(setAccessTokenMock).toHaveBeenCalledWith("recovered-token");
  });

  it("retries /auth/refresh on transient 5xx during restore", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiResponseError(503, "refresh_transient"))
      .mockResolvedValueOnce({ access_token: "recovered-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );
    expect(setAccessTokenMock).toHaveBeenCalledWith("recovered-token");
  });

  it("does NOT retry /auth/refresh on terminal 401 during restore", async () => {
    // A terminal 401 means the refresh cookie is dead — retrying just
    // wastes 750ms on the way to the login page.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"));

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    expect(screen.getByTestId("user")).toHaveTextContent("none");
    // Exactly two apiFetch calls: status + one refresh. No retries.
    expect(apiFetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries /auth/status on transient timeout", async () => {
    // /auth/status is the FIRST cold-start endpoint AuthProvider hits.
    // A timeout here used to cascade the whole restore flow into the
    // signed-out tree even when /auth/refresh would have succeeded.
    apiFetchMock
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "recovered-token" })
      .mockResolvedValueOnce(TEST_USER);

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email),
    );
  });

  it("gives up after 3 transient attempts and renders signed-out", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError());

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    expect(screen.getByTestId("user")).toHaveTextContent("none");
    // status + 3 refresh attempts = 4 total apiFetch calls.
    expect(apiFetchMock).toHaveBeenCalledTimes(4);
  });

  it("does not clear accessToken when /auth/me errors transiently after restore", async () => {
    // After a successful refresh, a transient /auth/me failure used to
    // null the accessToken — wasting the freshly-minted token and
    // forcing the next interaction to silently refresh again. The
    // fixed fetchMe only nulls accessToken on terminal 401/403.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockRejectedValueOnce(new ApiTimeoutError());

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"),
    );

    expect(screen.getByTestId("user")).toHaveTextContent("none");
    // setAccessToken called once with the restored token, then NOT
    // called again with null — the transient /me failure leaves the
    // session intact.
    expect(setAccessTokenMock).toHaveBeenCalledTimes(1);
    expect(setAccessTokenMock).toHaveBeenCalledWith("restored-token");
  });
});
