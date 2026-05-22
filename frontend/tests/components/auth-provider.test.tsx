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
  const { user, loading, needsSetup, billingUiEnabled, login, logout } = useAuth();
  const [error, setError] = useState("none");

  return (
    <div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="needs-setup">{String(needsSetup)}</div>
      <div data-testid="billing-ui-enabled">{String(billingUiEnabled)}</div>
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

  // ── 2026-05-18 P2 review fix: fetchMe contract on interactive login ──────

  it("login() retries /auth/me on transient timeout and succeeds", async () => {
    // /login succeeds. /auth/me times out twice then returns the user.
    // login() resolves and the caller can safely push /dashboard.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })                    // restore /status
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"))   // restore /refresh terminal
      .mockResolvedValueOnce({ access_token: "login-token" })           // login POST /login
      .mockRejectedValueOnce(new ApiTimeoutError())                     // login /me attempt 1
      .mockRejectedValueOnce(new ApiTimeoutError())                     // login /me attempt 2
      .mockResolvedValueOnce(TEST_USER);                                // login /me attempt 3

    render(<AuthProvider><Harness /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    fireEvent.click(screen.getByText("Login"));
    await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent(TEST_USER.email));
    // No error surfaced.
    expect(screen.getByTestId("error")).toHaveTextContent("none");
  });

  it("login() rejects on persistent transient /auth/me so caller doesn't push /dashboard with a null user", async () => {
    // /login succeeds. /auth/me times out 3 times (exhausts the retry
    // budget). fetchMe rethrows the transient error. login() rejects.
    // CRITICAL: setUser is NOT called with null (user state untouched)
    // and setAccessToken is NOT cleared (token may still be valid).
    // The caller (LoginPageBody) catches and shows an error message
    // instead of pushing /dashboard — which would have triggered
    // AppShell's `!loading && !user` redirect back to /login.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"))
      .mockResolvedValueOnce({ access_token: "login-token" })
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError());

    render(<AuthProvider><Harness /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    fireEvent.click(screen.getByText("Login"));
    // login() rejects with ApiTimeoutError → Harness catch sets error.
    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("ApiTimeoutError:"),
    );
    // User state remained null (initial state); accessToken stayed
    // SET at "login-token" — fetchMe must NOT have cleared either.
    expect(screen.getByTestId("user")).toHaveTextContent("none");
    // setAccessToken sequence: restore /refresh terminal → null,
    // then login /login → "login-token". fetchMe's persistent
    // transient handler does NOT call setAccessToken — the last
    // value must remain "login-token".
    expect(setAccessTokenMock).toHaveBeenCalledTimes(2);
    expect(setAccessTokenMock).toHaveBeenLastCalledWith("login-token");
  });

  it("login() rejects on terminal /auth/me 401 AND clears accessToken (real auth death)", async () => {
    // /login returned an access_token that /auth/me immediately
    // rejects with 401. Treat as terminal: clear accessToken + user,
    // rethrow. The login flow aborts; the caller's catch shows an
    // error.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockRejectedValueOnce(new ApiResponseError(401, "no session"))
      .mockResolvedValueOnce({ access_token: "login-token" })
      .mockRejectedValueOnce(new ApiResponseError(401, "user inactive"));

    render(<AuthProvider><Harness /></AuthProvider>);
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    fireEvent.click(screen.getByText("Login"));
    await waitFor(() =>
      expect(screen.getByTestId("error")).toHaveTextContent("ApiResponseError:user inactive"),
    );
    expect(screen.getByTestId("user")).toHaveTextContent("none");
    // setAccessToken sequence: restore /refresh terminal → null,
    // login /login → "login-token", terminal /me → null.
    expect(setAccessTokenMock).toHaveBeenCalledTimes(3);
    expect(setAccessTokenMock).toHaveBeenNthCalledWith(1, null);
    expect(setAccessTokenMock).toHaveBeenNthCalledWith(2, "login-token");
    expect(setAccessTokenMock).toHaveBeenNthCalledWith(3, null);
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

  it("keeps loading=true on persistent transient /auth/refresh", async () => {
    // Three transient /auth/refresh failures exhausts the retry
    // budget. The previous behaviour rendered the signed-out tree
    // (loading=false, user=null), which let AppShell redirect to
    // /login even though the refresh cookie may still be valid.
    // 2026-05-18 review fix: keep loading=true so the user sees the
    // spinner and can reload to retry; do not assume a transient
    // failure proves the session is dead.
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

    // Wait until all 4 calls completed (status + 3x refresh).
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(4));

    expect(screen.getByTestId("loading")).toHaveTextContent("true");
    expect(screen.getByTestId("user")).toHaveTextContent("none");
  });

  it("retries /auth/me on transient timeout during restore", async () => {
    // /auth/refresh succeeds. /auth/me times out twice then succeeds
    // on the third attempt. The user must end up signed in — a
    // single transient failure used to land them at /login.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError())
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
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
  });

  it("clears accessToken and signs out on terminal /auth/me 401 during restore", async () => {
    // A terminal 401 from /auth/me after a successful /auth/refresh
    // means the access token the refresh handed us is being rejected
    // by /me — treat as real logout: clear access token, set user to
    // null, allow AppShell to redirect.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
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
    // setAccessToken called twice: once with the restored token, then
    // with null (terminal /me => proper logout).
    expect(setAccessTokenMock).toHaveBeenCalledTimes(2);
    expect(setAccessTokenMock).toHaveBeenNthCalledWith(1, "restored-token");
    expect(setAccessTokenMock).toHaveBeenNthCalledWith(2, null);
  });

  // ── BILLING_UI_ENABLED kill switch flows from /auth/status into context ─

  it("defaults billingUiEnabled to false before /auth/status resolves", async () => {
    // Even before the status fetch settles, gated components must see
    // billingUiEnabled=false so the trial banner / settings tab stay
    // hidden on first render. (Pre-payment is the safe default.)
    apiFetchMock.mockImplementation(
      () => new Promise(() => {}), // never resolves
    );

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    expect(screen.getByTestId("billing-ui-enabled")).toHaveTextContent("false");
  });

  it("flows billing_ui_enabled=true from /auth/status into context", async () => {
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false, billing_ui_enabled: true })
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
    expect(screen.getByTestId("billing-ui-enabled")).toHaveTextContent("true");
  });

  it("treats missing billing_ui_enabled in status payload as false", async () => {
    // Defensive: an older API revision that doesn't include the new
    // key must keep the customer-facing billing surface hidden.
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
    expect(screen.getByTestId("billing-ui-enabled")).toHaveTextContent("false");
  });

  it("keeps loading=true on persistent transient /auth/me so AppShell doesn't redirect with a valid token", async () => {
    // Three transient /auth/me failures exhausts the retry budget.
    // CRITICAL: loading must STAY true (AppShell renders the spinner)
    // because we still have a freshly minted access token in memory —
    // dropping loading=false would let AppShell's `!loading && !user`
    // redirect fire and bounce the user to /login despite a healthy
    // session. The user can reload to retry; the access token is
    // preserved so the reload's silent refresh path is not forced.
    // 2026-05-18 review fix.
    apiFetchMock
      .mockResolvedValueOnce({ needs_setup: false })
      .mockResolvedValueOnce({ access_token: "restored-token" })
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError())
      .mockRejectedValueOnce(new ApiTimeoutError());

    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    // Wait until all 5 calls completed (status + refresh + 3x /me).
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(5));

    // accessToken set once with the restored token and NEVER cleared.
    expect(setAccessTokenMock).toHaveBeenCalledTimes(1);
    expect(setAccessTokenMock).toHaveBeenCalledWith("restored-token");
    // loading is still true so AppShell keeps the spinner up and
    // does NOT redirect to /login.
    expect(screen.getByTestId("loading")).toHaveTextContent("true");
    expect(screen.getByTestId("user")).toHaveTextContent("none");
  });
});
