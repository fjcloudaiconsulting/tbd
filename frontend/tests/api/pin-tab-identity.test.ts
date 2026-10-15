// TBD-623: a tab's identity is pinned at sign-in. A refresh that returns a
// token for a DIFFERENT (sub, org_id) is refused, never stored, never used
// to retry the caller's request. See specs/2026-10-15-tbd-623-pin-tab-identity.md
// (local). Fences F1-F8.
import {
  apiFetch,
  getAccessToken,
  setAccessToken,
  signInWithAccessToken,
} from "@/lib/api";

function b64(s: string): string {
  return btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}

function tok(
  claims: { sub?: string; org_id?: number; role?: string; iat?: number },
  expIn = 900,
): string {
  const payload = {
    ...claims,
    exp: Math.floor(Date.now() / 1000) + expIn,
  };
  return `${b64('{"alg":"HS256"}')}.${b64(JSON.stringify(payload))}.sig`;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const A = () => tok({ sub: "5", org_id: 7, role: "owner" });
const B = () => tok({ sub: "9", org_id: 11, role: "owner" });

describe("TBD-623 pinned tab identity", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const events: Array<{ type: string; detail: unknown }> = [];
  const record = (e: Event) =>
    events.push({ type: e.type, detail: (e as CustomEvent).detail });

  let refreshReturns: string;
  let bearers: Array<string | null>;
  let refreshCalls: number;

  /** accounts: 401 unless the bearer is `okFor`; refresh returns refreshReturns. */
  function wire(okFor: () => string | null) {
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      const auth = (init?.headers as Headers | undefined)?.get("Authorization") ?? null;
      if (url.includes("/api/v1/auth/refresh")) {
        refreshCalls++;
        return jsonResponse({ access_token: refreshReturns });
      }
      if (url.includes("/api/v1/auth/me")) return jsonResponse({}, 200);
      bearers.push(auth);
      return auth !== null && auth === `Bearer ${okFor()}`
        ? jsonResponse({ ok: true })
        : jsonResponse({ detail: "no" }, 401);
    });
  }

  const unauth = () => events.filter((e) => e.type === "auth:unauthenticated");

  beforeEach(() => {
    fetchMock.mockReset();
    events.length = 0;
    bearers = [];
    refreshCalls = 0;
    vi.stubGlobal("fetch", fetchMock);
    signInWithAccessToken(null);
    for (const t of ["auth:unauthenticated", "auth:refresh-attempt"]) {
      window.addEventListener(t, record);
    }
  });

  afterEach(() => {
    for (const t of ["auth:unauthenticated", "auth:refresh-attempt"]) {
      window.removeEventListener(t, record);
    }
    vi.useRealTimers();
    vi.unstubAllGlobals();
    signInWithAccessToken(null);
  });

  it("F1: 401 retry whose refresh returns another user is refused, dropped and reported", async () => {
    const a = A();
    const b = B();
    signInWithAccessToken(a);
    refreshReturns = b;
    wire(() => b);
    let tokenInsideAttemptListener: string | null | undefined = undefined;
    const peek = () => {
      tokenInsideAttemptListener = getAccessToken();
    };
    window.addEventListener("auth:refresh-attempt", peek);

    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    window.removeEventListener("auth:refresh-attempt", peek);

    expect(bearers).not.toContain(`Bearer ${b}`);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(1);
    expect(unauth()[0].detail).toEqual({ reason: "account_changed" });
    // Synchronously after the setter, B is already not the stored token.
    expect(tokenInsideAttemptListener).toBeNull(); // null (not undefined) proves the listener ran
  });

  it("F2: two concurrent 401s sharing one refresh that returns B, then a third call: B never on the wire, one event", async () => {
    const b = B();
    signInWithAccessToken(A());
    refreshReturns = b;
    wire(() => b);

    const results = await Promise.allSettled([
      apiFetch("/api/v1/accounts"),
      apiFetch("/api/v1/budgets"),
    ]);
    expect(results.every((r) => r.status === "rejected")).toBe(true);
    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });

    expect(refreshCalls).toBe(2); // ONE shared refresh for the two 401s, one for the third call
    expect(bearers).not.toContain(`Bearer ${b}`);
    expect(unauth()).toHaveLength(1);
  });

  it("F3: proactive timer refresh returning B drops the token once; the pin survives the drop; no transient retry storm", async () => {
    vi.useFakeTimers();
    const b = B();
    signInWithAccessToken(tok({ sub: "5", org_id: 7 }, 120)); // timer at exp-60s
    refreshReturns = b;
    wire(() => b);

    await vi.advanceTimersByTimeAsync(61_000);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(1);
    expect(refreshCalls).toBe(1);

    // A "foreign" kind that were transient would fire 2 more refreshes after
    // 250ms + 500ms of backoff.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(refreshCalls).toBe(1);

    // A later reactive refresh returning B is still refused: no retry, no event.
    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    expect(bearers).not.toContain(`Bearer ${b}`);
    expect(refreshCalls).toBe(2);
    expect(unauth()).toHaveLength(1);
    expect(getAccessToken()).toBeNull();
  });

  it("F3b: preflight refresh (near-expiry token) returning B drops the token once and never sends B", async () => {
    const b = B();
    signInWithAccessToken(tok({ sub: "5", org_id: 7 }, 30));
    refreshReturns = b;
    wire(() => b);

    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    expect(bearers).not.toContain(`Bearer ${b}`);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(1);
  });

  it("F4: same sub/org with different role, iat and exp is stored, the request retried, timer rescheduled, no event", async () => {
    vi.useFakeTimers();
    signInWithAccessToken(tok({ sub: "5", org_id: 7, role: "owner", iat: 1 }, 900));
    const a2 = tok({ sub: "5", org_id: 7, role: "member", iat: 2 }, 1800);
    refreshReturns = a2;
    wire(() => a2);

    await expect(apiFetch("/api/v1/accounts")).resolves.toEqual({ ok: true });

    expect(getAccessToken()).toBe(a2);
    expect(bearers.at(-1)).toBe(`Bearer ${a2}`);
    expect(unauth()).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(1); // proactive timer for the new exp
  });

  it("F5: same sub, different org_id is refused", async () => {
    signInWithAccessToken(tok({ sub: "5", org_id: 7 }));
    const other = tok({ sub: "5", org_id: 8 });
    refreshReturns = other;
    wire(() => other);

    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    expect(bearers).not.toContain(`Bearer ${other}`);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(1);
  });

  it("F6: a pinned identity refuses an undecodable refresh token", async () => {
    signInWithAccessToken(tok({ sub: "5", org_id: 7 }));
    refreshReturns = "opaque-not-a-jwt";
    wire(() => refreshReturns);

    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    expect(bearers).not.toContain(`Bearer opaque-not-a-jwt`);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(1);
  });

  it("F7: after a drop, a deliberate sign-in as B re-arms the pin and B's own refresh is accepted", async () => {
    const b = B();
    signInWithAccessToken(A());
    refreshReturns = b;
    wire(() => b);
    await expect(apiFetch("/api/v1/accounts")).rejects.toMatchObject({ status: 401 });
    expect(getAccessToken()).toBeNull();

    signInWithAccessToken(b);
    expect(getAccessToken()).toBe(b);

    const b2 = tok({ sub: "9", org_id: 11, role: "owner", iat: 99 });
    refreshReturns = b2;
    wire(() => b2);
    await expect(apiFetch("/api/v1/accounts")).resolves.toEqual({ ok: true });
    expect(getAccessToken()).toBe(b2);
  });

  it("F8: a manual clear is not an identity change: no event, and the pin stays armed", async () => {
    signInWithAccessToken(A());
    setAccessToken(null);
    expect(getAccessToken()).toBeNull();
    expect(unauth()).toHaveLength(0);
    // pin survives a null: another identity through the guarded setter is refused
    expect(setAccessToken(B())).toBe(false);
    expect(getAccessToken()).toBeNull();
  });

  it("setAccessToken returns true for null, for arming and for the same identity", () => {
    expect(setAccessToken(null)).toBe(true);
    expect(setAccessToken(A())).toBe(true);
    expect(setAccessToken(tok({ sub: "5", org_id: 7, role: "x" }))).toBe(true);
  });
});
