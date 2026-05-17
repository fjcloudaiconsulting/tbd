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

  it("apiFetch on /api/v1/auth/refresh succeeds when upstream resolves at 24s (under 25s budget)", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ access_token: "fresh-token" }), 24_000),
      );

      const promise = apiFetch<{ access_token: string }>("/api/v1/auth/refresh", {
        method: "POST",
      });

      // Advance the full 24s; upstream should resolve before the 25s
      // recovery timeout would have fired.
      await vi.advanceTimersByTimeAsync(24_000);

      await expect(promise).resolves.toEqual({ access_token: "fresh-token" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/refresh aborts at 25s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      // Upstream wouldn't resolve until 26s; AbortController fires at 25s.
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ access_token: "too-late" }), 26_000),
      );

      const promise = apiFetch<{ access_token: string }>("/api/v1/auth/refresh", {
        method: "POST",
      });
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
        message: "Request timed out. Try again.",
      });

      // 24999ms: still pending.
      await vi.advanceTimersByTimeAsync(24_999);
      // The 25000th ms: AbortController triggers, fetch rejects with
      // AbortError, fetchWithTimeout maps to ApiTimeoutError.
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/me succeeds when upstream resolves at 24s (under 25s budget)", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("valid-token");
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ id: 1, email: "x@y.z" }), 24_000),
      );

      const promise = apiFetch<{ id: number; email: string }>("/api/v1/auth/me");

      await vi.advanceTimersByTimeAsync(24_000);

      await expect(promise).resolves.toEqual({ id: 1, email: "x@y.z" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/me aborts at 25s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("valid-token");
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ id: 1 }), 26_000),
      );

      const promise = apiFetch("/api/v1/auth/me");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
      });

      await vi.advanceTimersByTimeAsync(24_999);
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
    // recovery path. /auth/status must share the 25s recovery
    // budget with /refresh and /me.
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ needs_setup: false }), 24_000),
      );

      const promise = apiFetch<{ needs_setup: boolean }>(
        "/api/v1/auth/status",
      );

      await vi.advanceTimersByTimeAsync(24_000);

      await expect(promise).resolves.toEqual({ needs_setup: false });
    } finally {
      vi.useRealTimers();
    }
  });

  it("apiFetch on /api/v1/auth/status aborts at 25s when upstream is still pending", async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(
        slowResponse(jsonResponse({ needs_setup: false }), 26_000),
      );

      const promise = apiFetch("/api/v1/auth/status");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
      });

      await vi.advanceTimersByTimeAsync(24_999);
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
    // 25s recovery budget) because the backend container was hibernating;
    // under the old 10s default this would have aborted and dispatched the
    // "refresh_transient" 503. Under the new budget the refresh succeeds
    // and the original request is retried with the fresh token.
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 401
        .mockImplementationOnce(
          slowResponse(jsonResponse({ access_token: "fresh-token" }), 24_000),
        )
        .mockResolvedValueOnce(jsonResponse({ items: [] }));                          // retry OK

      const promise = apiFetch<{ items: unknown[] }>("/api/v1/transactions");

      await vi.advanceTimersByTimeAsync(24_000);

      await expect(promise).resolves.toEqual({ items: [] });
      expect(dispatchEventSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("refresh-recovery path: 401 -> /refresh hangs past 25s -> recoverable 503 (terminal-budget case)", async () => {
    // Past the 25s budget the refresh still aborts -- this is the
    // recoverable transient case, not auth death.
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 30_000))
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 30_000))
        .mockImplementationOnce(slowResponse(jsonResponse({ access_token: "x" }), 30_000));

      const promise = apiFetch("/api/v1/protected");
      const assertion = expect(promise).rejects.toBeInstanceOf(ApiResponseError);

      // Advance through three 25s recovery timeouts + 250ms + 500ms backoffs.
      await vi.advanceTimersByTimeAsync(25_000);
      await vi.advanceTimersByTimeAsync(250);
      await vi.advanceTimersByTimeAsync(25_000);
      await vi.advanceTimersByTimeAsync(500);
      await vi.advanceTimersByTimeAsync(25_000);
      await assertion;

      await expect(promise).rejects.toMatchObject({
        status: 503,
        code: "refresh_transient",
      });
      expect(dispatchEventSpy).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
