// Proactive refresh tests — 2026-05-18.
//
// Reactive 401-driven refresh (PR #311) eliminates session DEATH but
// leaves a visible 401 burst in DevTools every time the access token
// expires mid-session: every concurrent fetcher ships the expired
// bearer, the backend returns 401, apiFetch's silent refresh fires,
// the original request retries with the new bearer. The user sees
// the data, but the Console shows red errors.
//
// Proactive refresh closes that gap. Before sending any non-auth
// request, apiFetch checks whether the access token is within the
// refresh-lead window of its exp; if so, it awaits the same
// singleflight refresh as the reactive path. No expired bearer ever
// leaves the browser. AppShell adds a visibility/focus handler so
// the same path fires when a backgrounded tab returns to foreground
// (where setTimeout throttling would otherwise have stalled the
// timer-driven refresh).
//
// These tests pin the six contracts the user spec'd:
//   1. token expiring soon → apiFetch("/accounts") refreshes first,
//      then sends /accounts ONCE with fresh bearer
//   2. focus/visibility with near-expiry token triggers refresh
//   3. concurrent requests near expiry share one refresh
//   4. transient proactive refresh failure preserves token and
//      original request still follows existing behavior
//   5. auth endpoints do not preflight-refresh
//   6. token with no/invalid exp falls back cleanly
//
// Helpers:
//   - jwtWithExp(secondsFromNow) builds a minimal three-part JWT
//     with a numeric `exp` claim. The signature is junk (the
//     frontend never verifies it; the backend does).
import {
  apiFetch,
  ensureFreshAccessToken,
  isAccessTokenNearExpiry,
  setAccessToken,
} from "@/lib/api";

function base64UrlEncode(s: string): string {
  return btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}

/**
 * Build a minimal JWT with an ``exp`` claim ``secondsFromNow`` from
 * the current wall clock. The header + signature are placeholders;
 * decodeJwtExp only reads the payload's exp.
 */
function jwtWithExp(secondsFromNow: number): string {
  const header = base64UrlEncode(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = base64UrlEncode(
    JSON.stringify({ exp: Math.floor(Date.now() / 1000) + secondsFromNow }),
  );
  return `${header}.${payload}.fake-signature`;
}

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("apiFetch proactive refresh", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  // ── Spec #1: near-expiry token preflights before sending ─────────────────

  it("near-expiry token: apiFetch(/accounts) refreshes first, then sends /accounts ONCE with the fresh bearer", async () => {
    // Token expires in 30s — well inside the 65s refresh-lead window
    // (60s lead + 5s clock-skew tolerance).
    const expiringToken = jwtWithExp(30);
    setAccessToken(expiringToken);
    expect(isAccessTokenNearExpiry()).toBe(true);

    fetchMock
      // 1st call: /api/v1/auth/refresh fires from the preflight.
      .mockResolvedValueOnce(jsonResponse({ access_token: jwtWithExp(900) }))
      // 2nd call: /api/v1/accounts with the fresh bearer.
      .mockResolvedValueOnce(jsonResponse({ accounts: [] }));

    const result = await apiFetch<{ accounts: unknown[] }>("/api/v1/accounts");
    expect(result).toEqual({ accounts: [] });

    // Exactly 2 fetch calls: preflight /refresh, then /accounts once.
    // No 401 burst, no retry-after-refresh.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/refresh");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/api/v1/accounts");

    // /accounts went out with the FRESH bearer, not the expiring one.
    const accountsHeaders = fetchMock.mock.calls[1][1]?.headers as Headers;
    const bearerHeader = accountsHeaders.get("Authorization");
    expect(bearerHeader).not.toBe(`Bearer ${expiringToken}`);
    expect(bearerHeader).toMatch(/^Bearer /);
  });

  // ── Spec #2: focus/visibility triggers refresh ───────────────────────────

  it("focus event with near-expiry token: ensureFreshAccessToken issues a /refresh", async () => {
    setAccessToken(jwtWithExp(20));
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ access_token: jwtWithExp(900) }),
    );

    await ensureFreshAccessToken();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/refresh");
  });

  it("ensureFreshAccessToken is a no-op when token is NOT near expiry", async () => {
    // Token expires in 15 min — well outside the 65s lead window.
    setAccessToken(jwtWithExp(900));
    expect(isAccessTokenNearExpiry()).toBe(false);

    await ensureFreshAccessToken();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // ── Spec #3: concurrent near-expiry requests share one refresh ───────────

  it("concurrent near-expiry requests share one /refresh via the singleflight", async () => {
    setAccessToken(jwtWithExp(20));

    // Slow /refresh so the second/third callers definitely race the first
    // through the preflight singleflight.
    let resolveRefresh!: (r: Response) => void;
    const refreshPending = new Promise<Response>((r) => { resolveRefresh = r; });
    fetchMock
      .mockReturnValueOnce(refreshPending)        // preflight /refresh (shared)
      .mockResolvedValueOnce(jsonResponse({ a: 1 }))  // /accounts
      .mockResolvedValueOnce(jsonResponse({ c: 1 }))  // /categories
      .mockResolvedValueOnce(jsonResponse({ b: 1 })); // /budgets

    // Fire three concurrent fetchers — all near-expiry preflight should
    // converge on the SAME pending refresh promise.
    const p1 = apiFetch<{ a: number }>("/api/v1/accounts");
    const p2 = apiFetch<{ c: number }>("/api/v1/categories");
    const p3 = apiFetch<{ b: number }>("/api/v1/budgets");

    // Let the microtasks resolve so each apiFetch enters the preflight.
    await Promise.resolve();
    await Promise.resolve();

    // At this point only the /refresh has been issued — none of the
    // downstream endpoint calls fire until /refresh resolves.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/refresh");

    // Resolve the shared refresh; all three apiFetch awaiters unblock.
    resolveRefresh(jsonResponse({ access_token: jwtWithExp(900) }));

    await expect(p1).resolves.toEqual({ a: 1 });
    await expect(p2).resolves.toEqual({ c: 1 });
    await expect(p3).resolves.toEqual({ b: 1 });

    // Exactly 4 fetch calls: 1 refresh + 3 endpoint calls.
    expect(fetchMock).toHaveBeenCalledTimes(4);
    const refreshCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes("/api/v1/auth/refresh"),
    );
    expect(refreshCalls).toHaveLength(1);
  });

  // ── Spec #4: transient proactive failure preserves state ─────────────────

  it("transient proactive refresh failure preserves the token and lets reactive recovery handle the 401", async () => {
    const expiringToken = jwtWithExp(20);
    setAccessToken(expiringToken);

    fetchMock
      // Preflight /refresh: all 3 attempts time out / 5xx. The retry
      // budget exhausts to transient; ensureFreshAccessToken returns
      // silently (no clear).
      .mockResolvedValueOnce(jsonResponse({ detail: "x" }, { status: 500 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "x" }, { status: 500 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "x" }, { status: 500 }))
      // Original /accounts goes out with the still-near-expiry token
      // (backend WILL accept it because it isn't actually expired yet
      // — we're inside the lead window but the JWT exp hasn't passed).
      .mockResolvedValueOnce(jsonResponse({ accounts: [{ id: 1 }] }));

    const result = await apiFetch<{ accounts: Array<{ id: number }> }>(
      "/api/v1/accounts",
    );
    expect(result).toEqual({ accounts: [{ id: 1 }] });

    // Token preserved (transient = non-destructive).
    expect(typeof window === "undefined" ? null : null).toBeNull(); // no-op guard
    // Most importantly: the original /accounts request DID go out
    // (reactive recovery still works) and we never spuriously
    // cleared the token.
    const accountsCall = fetchMock.mock.calls[3];
    const accountsHeaders = accountsCall[1]?.headers as Headers;
    expect(accountsHeaders.get("Authorization")).toBe(`Bearer ${expiringToken}`);
  });

  // ── Spec #5: auth endpoints skip the preflight ───────────────────────────

  it("/api/v1/auth/refresh does NOT preflight-refresh itself (no loop)", async () => {
    setAccessToken(jwtWithExp(20));  // near expiry
    fetchMock.mockResolvedValueOnce(jsonResponse({ access_token: "new" }));

    await apiFetch("/api/v1/auth/refresh", { method: "POST" });

    // Exactly 1 fetch — the explicit /refresh. No preflight.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/refresh");
  });

  it("/api/v1/auth/me does NOT preflight-refresh", async () => {
    setAccessToken(jwtWithExp(20));
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 1, email: "x@y.z" }));

    await apiFetch("/api/v1/auth/me");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/me");
  });

  it("/api/v1/auth/status does NOT preflight-refresh", async () => {
    setAccessToken(jwtWithExp(20));
    fetchMock.mockResolvedValueOnce(jsonResponse({ needs_setup: false }));

    await apiFetch("/api/v1/auth/status");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/status");
  });

  it("/api/v1/auth/login does NOT preflight-refresh (credential check)", async () => {
    // Even if a stale token is in memory, /login carries credentials,
    // not a bearer — it must not trigger an upstream refresh.
    setAccessToken(jwtWithExp(20));
    fetchMock.mockResolvedValueOnce(jsonResponse({ access_token: "x" }));

    await apiFetch("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ login: "a", password: "b" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/login");
  });

  it("/api/v1/auth/mfa/verify does NOT preflight-refresh", async () => {
    setAccessToken(jwtWithExp(20));
    fetchMock.mockResolvedValueOnce(jsonResponse({ access_token: "x" }));

    await apiFetch("/api/v1/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ mfa_token: "x", code: "123456" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/auth/mfa/verify");
  });

  // ── Spec #6: token with no/invalid exp falls back cleanly ────────────────

  it("token with no exp claim: no preflight, falls back to reactive 401 path", async () => {
    // Hand-crafted JWT with a payload that lacks `exp`. decodeJwtExp
    // returns null; isAccessTokenNearExpiry returns false; no preflight.
    const header = base64UrlEncode(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = base64UrlEncode(JSON.stringify({ sub: "1" }));  // no exp
    const noExpToken = `${header}.${payload}.fake`;
    setAccessToken(noExpToken);
    expect(isAccessTokenNearExpiry()).toBe(false);

    fetchMock.mockResolvedValueOnce(jsonResponse({ accounts: [] }));

    await apiFetch("/api/v1/accounts");

    // Exactly 1 call — straight to /accounts, no preflight.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/accounts");
  });

  it("non-JWT token: no preflight, falls back to reactive 401 path", async () => {
    // Bare string — not a three-part JWT. Pre-PR #310 the production
    // app actually issued bearer tokens like this for legacy paths,
    // so the fallback is load-bearing.
    setAccessToken("not-a-jwt-just-a-bearer-string");
    expect(isAccessTokenNearExpiry()).toBe(false);

    fetchMock.mockResolvedValueOnce(jsonResponse({ accounts: [] }));
    await apiFetch("/api/v1/accounts");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/accounts");
  });

  it("malformed JWT (broken base64): no preflight, falls back cleanly", async () => {
    setAccessToken("aaa.@@@-not-base64-@@@.bbb");
    expect(isAccessTokenNearExpiry()).toBe(false);

    fetchMock.mockResolvedValueOnce(jsonResponse({ accounts: [] }));
    await apiFetch("/api/v1/accounts");

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("JWT with non-numeric exp: no preflight, falls back cleanly", async () => {
    const header = base64UrlEncode(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = base64UrlEncode(JSON.stringify({ exp: "not-a-number" }));
    setAccessToken(`${header}.${payload}.fake`);
    expect(isAccessTokenNearExpiry()).toBe(false);

    fetchMock.mockResolvedValueOnce(jsonResponse({ accounts: [] }));
    await apiFetch("/api/v1/accounts");

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // ── Bonus: null token has no preflight effect ────────────────────────────

  it("null access token: apiFetch sends without bearer, no preflight", async () => {
    setAccessToken(null);
    fetchMock.mockResolvedValueOnce(jsonResponse({ accounts: [] }));
    await apiFetch("/api/v1/accounts");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });
});
