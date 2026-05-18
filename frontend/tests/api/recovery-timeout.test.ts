// Recovery-path timeout tests for apiFetch.
//
// PR fix(frontend) cold-start: the auth recovery routes (/api/v1/auth/refresh
// and /api/v1/auth/me) get a 25s timeout vs the 10s default that applies to
// every other path. The user's DO App Platform Basic-XS tier hibernates the
// backend container during idle; the first refresh after idle takes longer
// than 10s for TLS handshake + cold container boot. The longer budget lets
// the recovery path succeed instead of false-positiving the global 10s
// timeout and surfacing "Session refresh temporarily unavailable".
//
// Non-recovery paths are unaffected and still abort at 10s.

import { ApiResponseError, apiFetch, setAccessToken } from "@/lib/api";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("apiFetch recovery-path timeout", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const dispatchEventSpy = vi.spyOn(window, "dispatchEvent");

  beforeEach(() => {
    fetchMock.mockReset();
    dispatchEventSpy.mockClear();
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken(null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  afterAll(() => {
    dispatchEventSpy.mockRestore();
  });

  // Returns a fetch mock that resolves with ``response`` after ``delayMs``
  // of fake-timer elapsed time, OR rejects with AbortError if the signal
  // fires first. Mirrors how a real slow upstream behaves when the
  // AbortController kicks in.
  function slowResponse(response: Response, delayMs: number) {
    return (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const signal = init?.signal;
        const timer = setTimeout(() => resolve(response), delayMs);
        signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
  }

  it("apiFetch on /api/v1/auth/refresh succeeds when upstream resolves at 24s (under 45s budget)", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ access_token: "fresh-token" }), 44_000),
      );

      const promise = apiFetch<{ access_token: string }>("/api/v1/auth/refresh", {
        method: "POST",
      });

      // Advance the full 24s; upstream should resolve before the 25s
      // recovery timeout would have fired.
      await vi.advanceTimersByTimeAsync(44_000);

      await expect(promise).resolves.toEqual({ access_token: "fresh-token" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/refresh aborts at 45s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      // Upstream wouldn't resolve until 26s; AbortController fires at 25s.
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ access_token: "too-late" }), 46_000),
      );

      const promise = apiFetch<{ access_token: string }>("/api/v1/auth/refresh", {
        method: "POST",
      });
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
        message: "Request timed out. Try again.",
      });

      // 24999ms: still pending.
      await vi.advanceTimersByTimeAsync(44_999);
      // The 25000th ms: AbortController triggers, fetch rejects with
      // AbortError, fetchWithTimeout maps to ApiTimeoutError.
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/me succeeds when upstream resolves at 24s (under 45s budget)", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("valid-token");
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ id: 1, email: "x@y.z" }), 44_000),
      );

      const promise = apiFetch<{ id: number; email: string }>("/api/v1/auth/me");

      await vi.advanceTimersByTimeAsync(44_000);

      await expect(promise).resolves.toEqual({ id: 1, email: "x@y.z" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/me aborts at 45s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("valid-token");
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ id: 1 }), 46_000),
      );

      const promise = apiFetch("/api/v1/auth/me");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
      });

      await vi.advanceTimersByTimeAsync(44_999);
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/status succeeds when upstream resolves at 24s (architect P1 on PR #309 — cold-start restore)", async () => {
    // AuthProvider's first call on mount is /api/v1/auth/status. If
    // that times out at 10s on a cold container, the restore chain
    // (status → refresh → me) never reaches /refresh and the user
    // sees a generic 503 from the unauthed path instead of the
    // recovery path. /auth/status must share the 45s recovery
    // budget with /refresh and /me.
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ needs_setup: false }), 44_000),
      );

      const promise = apiFetch<{ needs_setup: boolean }>(
        "/api/v1/auth/status",
      );

      await vi.advanceTimersByTimeAsync(44_000);

      await expect(promise).resolves.toEqual({ needs_setup: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/status aborts at 45s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ needs_setup: false }), 46_000),
      );

      const promise = apiFetch("/api/v1/auth/status");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
      });

      await vi.advanceTimersByTimeAsync(44_999);
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("non-recovery path /api/v1/transactions still aborts at 10s (longer budget is recovery-only)", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("valid-token");
      // Upstream would resolve at 11s but the default 10s aborts first.
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ items: [] }), 11_000),
      );

      const promise = apiFetch("/api/v1/transactions");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
      });

      // 9999ms: still pending.
      await vi.advanceTimersByTimeAsync(9_999);
      // The 10000th ms: AbortController triggers at the default budget.
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("refresh-recovery path: 401 -> /refresh resolves at 24s -> retry succeeds (cold-start scenario)", async () => {
    // Belt-and-braces realism: a primary 401 on /api/v1/transactions triggers
    // the silent /refresh flow. The /refresh call takes 24s (just under the
    // 45s recovery budget) because the backend container was hibernating;
    // under the old 10s default this would have aborted and dispatched the
    // "refresh_transient" 503. Under the new budget the refresh succeeds
    // and the original request is retried with the fresh token.
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 401
        .mockImplementationOnce(
          slowResponse(jsonResponse({ access_token: "fresh-token" }), 44_000),
        )
        .mockResolvedValueOnce(jsonResponse({ items: [] }));                          // retry OK

      const promise = apiFetch<{ items: unknown[] }>("/api/v1/transactions");

      await vi.advanceTimersByTimeAsync(44_000);

      await expect(promise).resolves.toEqual({ items: [] });
      // 2026-05-18: the new auth:refresh-attempt + auth:retry-after-
      // refresh events DO fire (logging observability), but the
      // pre-existing intent of this assertion was "no
      // auth:unauthenticated escapes when the session is alive."
      // Narrow accordingly.
      expect(
        dispatchEventSpy.mock.calls.some(
          ([e]) => (e as Event).type === "auth:unauthenticated",
        ),
      ).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("refresh-recovery path: 401 -> /refresh hangs past 45s -> recoverable 503 (terminal-budget case)", async () => {
    // Past the 45s budget the refresh still aborts -- this is the
    // recoverable transient case, not auth death.
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 50_000))
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 50_000))
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 50_000));

      const promise = apiFetch("/api/v1/protected");
      const assertion = expect(promise).rejects.toBeInstanceOf(ApiResponseError);

      // Advance through three 45s recovery timeouts + 250ms + 500ms backoffs.
      await vi.advanceTimersByTimeAsync(45_000);
      await vi.advanceTimersByTimeAsync(250);
      await vi.advanceTimersByTimeAsync(45_000);
      await vi.advanceTimersByTimeAsync(500);
      await vi.advanceTimersByTimeAsync(45_000);
      await assertion;

      await expect(promise).rejects.toMatchObject({
        status: 503,
        code: "refresh_transient",
      });
      // Transient refresh exhaustion must NOT escalate to logout.
      // The new auth:refresh-attempt events fire as observability;
      // we only care that auth:unauthenticated did not.
      expect(
        dispatchEventSpy.mock.calls.some(
          ([e]) => (e as Event).type === "auth:unauthenticated",
        ),
      ).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  // ── 2026-05-18 idle-recovery: 28s tail replay regression ─────────────────
  //
  // Production log (deployment 7506c8ff at 10:32:55 GMT, basic-xxs
  // backend, ~30 min user idle) showed:
  //
  //   10:32:55.226  GET  /api/v1/accounts     401  (access token expired)
  //   10:32:55.332  GET  /api/v1/categories   401
  //   10:32:55.446  GET  /api/v1/categories   401
  //   10:33:23.785  POST /api/v1/auth/refresh 200  (28s after the 401s)
  //
  // Under the previous 25s recovery budget the browser aborted the
  // refresh fetch BEFORE the 28s backend response arrived; the
  // backend logged 200 (orphaned) and the frontend logged a transient
  // 503. apiFetch's retry-after-refresh path never fired because
  // refreshResult.ok was never true on the first attempt — the
  // singleflight saw a transient, and the original 401'd /accounts
  // and /categories requests surfaced 503s into page-level silent
  // .catch(() => {}) handlers.
  //
  // Under the new 45s budget, a 28s upstream resolves within budget,
  // refreshAccessTokenOnce returns ok, the original request retries
  // with the fresh bearer, AND the auth:retry-after-refresh event
  // fires with the retry's 200 status. This test pins that contract
  // end-to-end so a future regression of the timeout knob is caught.

  it("28s observed tail: 401 -> /refresh resolves at 28s -> original request replays with new bearer", async () => {
    vi.useFakeTimers();
    const events: Array<{ type: string; detail: unknown }> = [];
    const recorder = (e: Event) => {
      if (
        e.type === "auth:refresh-attempt"
        || e.type === "auth:retry-after-refresh"
      ) {
        events.push({ type: e.type, detail: (e as CustomEvent).detail });
      }
    };
    window.addEventListener("auth:refresh-attempt", recorder);
    window.addEventListener("auth:retry-after-refresh", recorder);

    try {
      setAccessToken("stale-token");
      fetchMock
        // 1) Original /api/v1/accounts → 401 (access token expired
        //    after ~15 min idle on basic-xxs).
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        // 2) Silent /refresh → resolves at 28s, just under the new
        //    45s recovery budget but well past the old 25s.
        .mockImplementationOnce(
          slowResponse(jsonResponse({ access_token: "fresh-token" }), 28_000),
        )
        // 3) Retry of /accounts with new bearer → 200.
        .mockResolvedValueOnce(jsonResponse({ accounts: [{ id: 1, name: "Checking" }] }));

      const promise = apiFetch<{ accounts: Array<{ id: number; name: string }> }>(
        "/api/v1/accounts",
      );

      await vi.advanceTimersByTimeAsync(28_000);

      await expect(promise).resolves.toEqual({
        accounts: [{ id: 1, name: "Checking" }],
      });

      // The retry MUST have actually hit the network with the new
      // bearer. Three fetch calls total: original, refresh, retry.
      expect(fetchMock).toHaveBeenCalledTimes(3);
      const retryHeaders = fetchMock.mock.calls[2]?.[1]?.headers as Headers;
      expect(retryHeaders.get("Authorization")).toBe("Bearer fresh-token");

      // Observability: exactly one auth:refresh-attempt with outcome
      // "ok" (the single attempt succeeded — no retries), and exactly
      // one auth:retry-after-refresh with status 200.
      const refreshEvents = events.filter((e) => e.type === "auth:refresh-attempt");
      expect(refreshEvents).toHaveLength(1);
      expect(refreshEvents[0].detail).toMatchObject({
        attempt: 1,
        outcome: "ok",
      });
      const retryEvents = events.filter((e) => e.type === "auth:retry-after-refresh");
      expect(retryEvents).toHaveLength(1);
      expect(retryEvents[0].detail).toMatchObject({
        path: "/api/v1/accounts",
        status: 200,
        ok: true,
      });
    } finally {
      window.removeEventListener("auth:refresh-attempt", recorder);
      window.removeEventListener("auth:retry-after-refresh", recorder);
      vi.useRealTimers();
    }
  });

  it("refresh outcome events: each attempt dispatches auth:refresh-attempt with attempt index", async () => {
    // Three transient outcomes (timeout, timeout, success) must
    // dispatch three auth:refresh-attempt events with attempts
    // 1, 2, 3 respectively.
    vi.useFakeTimers();
    const refreshEvents: Array<{ attempt: number; outcome: string }> = [];
    const recorder = (e: Event) => {
      const detail = (e as CustomEvent).detail as { attempt: number; outcome: string };
      refreshEvents.push({ attempt: detail.attempt, outcome: detail.outcome });
    };
    window.addEventListener("auth:refresh-attempt", recorder);

    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        // Attempt 1: aborts at 45s timeout.
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 50_000))
        // Attempt 2: aborts at 45s timeout.
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 50_000))
        // Attempt 3: succeeds.
        .mockResolvedValueOnce(jsonResponse({ access_token: "fresh" }))
        // Retry of /protected with fresh bearer.
        .mockResolvedValueOnce(jsonResponse({ items: [] }));

      const promise = apiFetch("/api/v1/protected");

      await vi.advanceTimersByTimeAsync(45_000);  // attempt 1 abort
      await vi.advanceTimersByTimeAsync(250);     // backoff
      await vi.advanceTimersByTimeAsync(45_000);  // attempt 2 abort
      await vi.advanceTimersByTimeAsync(500);     // backoff
      // attempt 3 resolves synchronously (mockResolvedValueOnce).

      await expect(promise).resolves.toEqual({ items: [] });

      expect(refreshEvents).toEqual([
        { attempt: 1, outcome: "transient" },
        { attempt: 2, outcome: "transient" },
        { attempt: 3, outcome: "ok" },
      ]);
    } finally {
      window.removeEventListener("auth:refresh-attempt", recorder);
      vi.useRealTimers();
    }
  });

  it("retry-after-refresh event carries the original path AND the retry status", async () => {
    // Even when the retry comes back non-2xx (rare but possible —
    // e.g. backend authz changed mid-refresh), the event should
    // surface that so prod can spot it.
    vi.useFakeTimers();
    const events: Array<{ type: string; detail: unknown }> = [];
    const recorder = (e: Event) => {
      events.push({ type: e.type, detail: (e as CustomEvent).detail });
    };
    window.addEventListener("auth:retry-after-refresh", recorder);

    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
        .mockResolvedValueOnce(jsonResponse({ detail: "forbidden" }, { status: 403 }));

      const promise = apiFetch("/api/v1/admin/orgs");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiResponseError",
        status: 403,
      });
      await vi.advanceTimersByTimeAsync(0);
      await assertion;

      expect(events).toHaveLength(1);
      expect(events[0].detail).toMatchObject({
        path: "/api/v1/admin/orgs",
        status: 403,
        ok: false,
      });
    } finally {
      window.removeEventListener("auth:retry-after-refresh", recorder);
      vi.useRealTimers();
    }
  });

  // ── 2026-05-18 idle-recovery P2 review fix: PII redaction ────────────────
  //
  // Authenticated paths may include user-entered values in the query
  // string (e.g. /api/v1/transactions?q=<search term> carries the
  // transactions filter the user typed; /api/v1/categories?name=<...>
  // carries a category name; etc.). The retry-after-refresh event
  // MUST strip the query string + fragment before dispatch so the
  // detail never carries user input to telemetry consumers. The
  // route signature (pathname) is enough for ops triage.

  it("retry-after-refresh strips query string from path (PII redaction)", async () => {
    vi.useFakeTimers();
    const events: Array<{ path: string }> = [];
    const recorder = (e: Event) => {
      const detail = (e as CustomEvent).detail as { path: string };
      events.push({ path: detail.path });
    };
    window.addEventListener("auth:retry-after-refresh", recorder);

    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
        .mockResolvedValueOnce(jsonResponse({ items: [] }));

      // Path carries a search term that should NEVER reach telemetry.
      const promise = apiFetch(
        "/api/v1/transactions?q=mortgage-payment&date_from=2026-01-01",
      );
      await vi.advanceTimersByTimeAsync(0);
      await expect(promise).resolves.toEqual({ items: [] });

      expect(events).toHaveLength(1);
      // Pathname only — no query string, no fragment.
      expect(events[0].path).toBe("/api/v1/transactions");
      expect(events[0].path).not.toContain("?");
      expect(events[0].path).not.toContain("mortgage-payment");
      expect(events[0].path).not.toContain("date_from");
    } finally {
      window.removeEventListener("auth:retry-after-refresh", recorder);
      vi.useRealTimers();
    }
  });

  it("retry-after-refresh strips URL fragment too", async () => {
    // Fragments don't reach the server, but a caller could plausibly
    // pass one (e.g. a deep-link route). Defence-in-depth: strip it.
    vi.useFakeTimers();
    const events: Array<{ path: string }> = [];
    const recorder = (e: Event) => {
      const detail = (e as CustomEvent).detail as { path: string };
      events.push({ path: detail.path });
    };
    window.addEventListener("auth:retry-after-refresh", recorder);

    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
        .mockResolvedValueOnce(jsonResponse({ ok: true }));

      const promise = apiFetch("/api/v1/categories#sensitive-anchor");
      await vi.advanceTimersByTimeAsync(0);
      await promise;

      expect(events).toHaveLength(1);
      expect(events[0].path).toBe("/api/v1/categories");
      expect(events[0].path).not.toContain("#");
    } finally {
      window.removeEventListener("auth:retry-after-refresh", recorder);
      vi.useRealTimers();
    }
  });
});
