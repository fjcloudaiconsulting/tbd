// TBD-625: a backend restart must not end a valid session. Runs the REAL
// lib/api (apiFetch, refresh, error classification); only `fetch` is stubbed,
// so each case is the response the browser actually gets while the edge proxy
// cannot reach the backend.
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, useAuth } from "@/components/auth/AuthProvider";
import { signInWithAccessToken } from "@/lib/api";

function b64(s: string): string {
  return btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}
const TOKEN = `${b64("{}")}.${b64(JSON.stringify({ sub: "5", org_id: 7, exp: 4102444800 }))}.s`;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// The three shapes of "backend not there yet".
const REFUSED = () => Promise.reject(new TypeError("Failed to fetch"));
const BAD_GATEWAY_PAGE = () =>
  Promise.resolve(
    new Response("<!DOCTYPE html><html><body><h1>502 Bad Gateway</h1></body></html>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "Content-Type": "text/html" },
    }),
  );
const HTML_200 = () =>
  Promise.resolve(
    new Response("<!DOCTYPE html><html><body>Origin unreachable</body></html>", {
      status: 200,
      headers: { "Content-Type": "text/html" },
    }),
  );

// Every render, recorded. AppShell redirects to /login on `!loading && !user`.
const seen: string[] = [];
function Harness() {
  const { user, loading } = useAuth();
  seen.push(!loading && !user ? "signed-out" : user ? "signed-in" : "loading");
  return <div data-testid="user">{user?.email ?? "none"}</div>;
}

// Signs in and out like the login form and the user menu do.
function Controls() {
  const { user, loading, login, logout } = useAuth();
  return (
    <div>
      <div data-testid="who">{user?.email ?? "none"}</div>
      <div data-testid="state">{loading ? "loading" : user ? "signed-in" : "signed-out"}</div>
      <button onClick={() => login("owner", "pw").catch(() => {})}>Login</button>
      <button onClick={() => logout()}>Logout</button>
    </div>
  );
}

// The backend fails `failures` requests to `down` the given way, then serves.
function backend(
  down: "/auth/status" | "/auth/refresh",
  fail: () => Promise<Response>,
  failures: number,
  refreshStatus = 200,
) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input).replace(/^.*\/api\/v1/, "");
    calls.push(path);
    if (path === down && failures > 0) {
      failures--;
      return fail();
    }
    if (path === "/auth/status") return json({ needs_setup: false });
    if (path === "/auth/refresh") {
      return refreshStatus === 200
        ? json({ access_token: TOKEN })
        : json({ detail: "Invalid refresh token" }, refreshStatus);
    }
    const authed = new Headers(init?.headers).get("Authorization") === `Bearer ${TOKEN}`;
    if (path === "/auth/me") {
      return authed
        ? json({ id: 5, org_id: 7, email: "owner@example.com" })
        : json({ detail: "Not authenticated" }, 401);
    }
    throw new Error(`unexpected ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

describe("AuthProvider across a backend restart (TBD-625)", () => {
  beforeEach(() => {
    seen.length = 0;
    signInWithAccessToken(null);
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    signInWithAccessToken(null);
  });

  // Longer than withAuthRetry's three attempts, as a restart is.
  const OUTAGE = 6;

  it.each([
    ["connection refused during /auth/status", "/auth/status", REFUSED],
    ["a 502 HTML page during /auth/status", "/auth/status", BAD_GATEWAY_PAGE],
    ["a 200 non-JSON page during /auth/status", "/auth/status", HTML_200],
    ["connection refused during /auth/refresh", "/auth/refresh", REFUSED],
    ["a 502 HTML page during /auth/refresh", "/auth/refresh", BAD_GATEWAY_PAGE],
  ] as const)("%s: stays signed in and recovers without a reload", async (_name, down, fail) => {
    backend(down, fail, OUTAGE);
    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    for (let i = 0; i < 6; i++) {
      await act(() => vi.advanceTimersByTimeAsync(10_000));
    }

    await waitFor(() =>
      expect(screen.getByTestId("user")).toHaveTextContent("owner@example.com"),
    );
    expect(seen).not.toContain("signed-out");
  });

  it("a real 401 from /auth/refresh still ends the session, and nothing retries it", async () => {
    // A transient first, so the 401 lands on a later attempt.
    const calls = backend("/auth/refresh", REFUSED, 1, 401);
    render(
      <AuthProvider>
        <Harness />
      </AuthProvider>,
    );

    await act(() => vi.advanceTimersByTimeAsync(1_000));
    await waitFor(() => expect(seen.at(-1)).toBe("signed-out"));
    const settled = calls.length;

    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(calls).toHaveLength(settled);
    expect(screen.getByTestId("user")).toHaveTextContent("none");
  });

  describe("an explicit sign-in or sign-out stops the restore loop", () => {
    // Paths in `down` are refused; everything else is served.
    const down = new Set<string>();
    let calls: string[] = [];
    const restoreCalls = () =>
      calls.filter((p) => p === "/auth/status" || p === "/auth/refresh").length;

    beforeEach(() => {
      down.clear();
      calls = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
          const path = String(input).replace(/^.*\/api\/v1/, "");
          calls.push(path);
          if (down.has(path)) throw new TypeError("Failed to fetch");
          if (path === "/auth/status") return json({ needs_setup: false });
          if (path === "/auth/refresh" || path === "/auth/login") {
            return json({ access_token: TOKEN });
          }
          if (path === "/auth/logout") return json({ ok: true });
          const authed = new Headers(init?.headers).get("Authorization") === `Bearer ${TOKEN}`;
          if (path === "/auth/me" && authed) {
            return json({ id: 5, org_id: 7, email: "owner@example.com" });
          }
          return json({ detail: "Not authenticated" }, 401);
        }),
      );
    });

    // Restore has spent its first budget on a refused /auth/refresh and its
    // next pass is pending; the backend then answers just long enough for
    // an interactive sign-in.
    async function signInDuringOutage() {
      down.add("/auth/refresh");
      render(
        <AuthProvider>
          <Controls />
        </AuthProvider>,
      );
      await act(() => vi.advanceTimersByTimeAsync(800));
      expect(screen.getByTestId("state")).toHaveTextContent("loading");
      fireEvent.click(screen.getByText("Login"));
      await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("owner@example.com"));
    }

    it("login(): the pending loop sends no more restore requests", async () => {
      await signInDuringOutage();
      const before = restoreCalls();

      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(restoreCalls()).toBe(before);
      expect(screen.getByTestId("state")).toHaveTextContent("signed-in");
    });

    it("logout(): the loop cannot sign the user back in once the backend recovers", async () => {
      await signInDuringOutage();
      // Backend drops again: the logout POST fails, so the refresh cookie
      // would survive on the server side.
      down.add("/auth/logout");
      fireEvent.click(screen.getByText("Logout"));
      await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("none"));
      const before = restoreCalls();

      down.clear();
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(restoreCalls()).toBe(before);
      expect(screen.getByTestId("who")).toHaveTextContent("none");
      // Not parked on the spinner either: AppShell can send them to /login.
      expect(screen.getByTestId("state")).toHaveTextContent("signed-out");
    });
  });
});
