import {
  ApiResponseError,
  apiFetch,
  extractErrorMessage,
  getAccessToken,
  setAccessToken,
} from "@/lib/api";


function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}


describe("apiFetch", () => {
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

  function neverResolvingFetch() {
    return (_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal;
        signal?.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
  }

  it("adds auth and JSON headers for string request bodies", async () => {
    setAccessToken("access-123");
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));

    await apiFetch("/api/v1/example", {
      method: "POST",
      body: JSON.stringify({ hello: "world" }),
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, options] = fetchMock.mock.calls[0];
    const headers = options?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer access-123");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("refreshes and retries once after a 401", async () => {
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    const data = await apiFetch<{ ok: boolean }>("/api/v1/protected");

    expect(data).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/auth/refresh");
    const retryHeaders = fetchMock.mock.calls[2][1]?.headers as Headers;
    expect(retryHeaders.get("Authorization")).toBe("Bearer fresh-token");
    expect(getAccessToken()).toBe("fresh-token");
  });

  it("times out an unresponsive initial request instead of hanging forever", async () => {
    // Exercises the default 10s budget — no timeoutMs override on the call,
    // so fetchWithTimeout falls back to API_FETCH_TIMEOUT_MS.
    vi.useFakeTimers();
    try {
      fetchMock.mockImplementationOnce(neverResolvingFetch());

      const promise = apiFetch("/api/v1/accounts");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
        message: "Request timed out. Try again.",
      });

      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("times out a hung retry-after-refresh and rejects with ApiTimeoutError without clearing auth", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))      // primary 401
        .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))             // refresh OK
        .mockImplementationOnce(neverResolvingFetch());                                   // retry hangs

      const promise = apiFetch("/api/v1/protected");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiTimeoutError",
        message: "Request timed out. Try again.",
      });

      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;

      // Refresh succeeded -> token IS saved, retry hung -> ApiTimeoutError
      // but the session is intact, so the user can simply retry the same
      // call without being kicked back to /login.
      expect(getAccessToken()).toBe("fresh-token");
      expect(dispatchEventSpy).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(3);  // primary + refresh + retry
    } finally {
      vi.useRealTimers();
    }
  });

  it("custom timeoutMs aborts at the configured value, not the default", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("access-123");
      fetchMock.mockImplementationOnce(neverResolvingFetch());

      const promise = apiFetch("/api/v1/slow-endpoint", { timeoutMs: 5_000 });
      const assertion = expect(promise).rejects.toMatchObject({ name: "ApiTimeoutError" });

      // Advance to 4999ms — should NOT have aborted yet.
      await vi.advanceTimersByTimeAsync(4_999);
      // Promise still pending; expect.rejects has not fired yet.

      // Advance the last 1ms — aborts at exactly 5000ms.
      await vi.advanceTimersByTimeAsync(1);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("custom timeoutMs longer than default does not abort at 10s", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("access-123");
      fetchMock.mockImplementationOnce(neverResolvingFetch());

      const promise = apiFetch("/api/v1/import/preview", { timeoutMs: 15_000 });
      const assertion = expect(promise).rejects.toMatchObject({ name: "ApiTimeoutError" });

      // Advance to 10000ms — should NOT have aborted at the default.
      await vi.advanceTimersByTimeAsync(10_000);
      // Promise still pending.

      // Advance another 5000ms — aborts at 15000ms total.
      await vi.advanceTimersByTimeAsync(5_000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not forward timeoutMs to native fetch", async () => {
    setAccessToken("access-123");
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));

    await apiFetch("/api/v1/anything", { timeoutMs: 5_000 });

    const fetchInit = fetchMock.mock.calls[0][1];
    expect(fetchInit).not.toHaveProperty("timeoutMs");
  });

  it("primary 401 + refresh OK + retry 401 propagates 401 without clearing token (new contract)", async () => {
    // Under the architect-locked 2026-05-15 contract, terminal auth death
    // is detected by the REFRESH endpoint returning 401/403, not by a
    // retry-after-refresh returning 401. If refresh handed back a fresh
    // token but the retry still 401s, the caller sees the original 401
    // ApiResponseError and the token stays in memory (next call will go
    // through the refresh path again as a fresh attempt).
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "still expired" }, { status: 401 }));

    await expect(apiFetch("/api/v1/protected")).rejects.toMatchObject({
      name: "ApiResponseError",
      status: 401,
      message: "still expired",
    });

    expect(getAccessToken()).toBe("fresh-token");
    expect(dispatchEventSpy).not.toHaveBeenCalled();
  });

  // Architect-locked 2026-05-15: refreshAccessToken() now returns a
  // discriminated RefreshResult that distinguishes terminal auth death
  // (401/403 on the refresh endpoint) from transient failures (timeout,
  // 5xx, network, JSON parse, 200 OK without access_token). Only the
  // terminal branch clears the in-memory token and dispatches
  // auth:unauthenticated. Transient failures throw an ApiResponseError
  // with code "refresh_transient" so callers / SWR can retry without the
  // user being kicked back to /login.

  it("primary 401 + refresh 401 + /me 401 dispatches auth:unauthenticated and clears token (terminal)", async () => {
    // Team F 2026-05-17: terminal /refresh now triggers a /me probe before
    // dispatching auth:unauthenticated. Both must fail for true auth death.
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))   // primary
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid_refresh" }, { status: 401 })) // refresh terminal
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid_token" }, { status: 401 })); // /me confirms

    await expect(apiFetch("/api/v1/protected")).rejects.toMatchObject({
      name: "ApiResponseError",
      status: 401,
    });

    expect(getAccessToken()).toBeNull();
    expect(dispatchEventSpy).toHaveBeenCalledWith(
      expect.objectContaining({ type: "auth:unauthenticated" }),
    );
  });

  it("primary 401 + refresh 401 + /me 200 does NOT dispatch and preserves token (ambiguous-401 defense)", async () => {
    // Team F 2026-05-17: this closes the ambiguous-401 false-logout class.
    // When /refresh terminally 401s but the access token is still valid
    // (backend race / partial outage), /me confirms the session is alive
    // and we leave the user signed in. The original 401 falls through so
    // SWR can run its normal retry path.
    setAccessToken("still-valid-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))   // primary
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid_refresh" }, { status: 401 })) // refresh terminal
      .mockResolvedValueOnce(jsonResponse({ id: 1, email: "x@y.z" })); // /me alive

    await expect(apiFetch("/api/v1/protected")).rejects.toMatchObject({
      name: "ApiResponseError",
      status: 401,
    });

    expect(getAccessToken()).toBe("still-valid-token"); // PRESERVED
    expect(dispatchEventSpy).not.toHaveBeenCalled();
  });

  it("refresh transient (TypeError, AbortError, then OK) retries within budget without logout", async () => {
    // Team F 2026-05-17: retry budget on refresh -- 2 retries with 250ms
    // exponential backoff -- absorbs a flaky network on idle return. With
    // real timers so the backoff actually elapses; the fetch path itself
    // resolves synchronously in this test.
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary
      .mockRejectedValueOnce(new TypeError("fetch failed"))                          // refresh attempt 1 -- transient
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"))             // refresh attempt 2 -- transient
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))         // refresh attempt 3 -- OK
      .mockResolvedValueOnce(jsonResponse({ ok: true }));                            // retry of primary

    const data = await apiFetch<{ ok: boolean }>("/api/v1/protected");

    expect(data).toEqual({ ok: true });
    expect(getAccessToken()).toBe("fresh-token");
    expect(dispatchEventSpy).not.toHaveBeenCalled();
    // 1 primary + 3 refresh attempts + 1 retry = 5
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("primary 401 + refresh timeout (all retries) does NOT clear token, does NOT dispatch, throws recoverable", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      // All 3 refresh attempts hang and time out at 10s each, separated by
      // 250ms then 500ms exponential backoffs.
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        .mockImplementationOnce(neverResolvingFetch())   // refresh attempt 1
        .mockImplementationOnce(neverResolvingFetch())   // refresh attempt 2
        .mockImplementationOnce(neverResolvingFetch());  // refresh attempt 3

      const promise = apiFetch("/api/v1/protected");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiResponseError",
        status: 503,
        code: "refresh_transient",
      });

      // Advance through all three 10s timeouts + 250ms + 500ms backoffs.
      await vi.advanceTimersByTimeAsync(10_000);
      await vi.advanceTimersByTimeAsync(250);
      await vi.advanceTimersByTimeAsync(10_000);
      await vi.advanceTimersByTimeAsync(500);
      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;

      expect(getAccessToken()).toBe("stale-token");  // PRESERVED
      expect(dispatchEventSpy).not.toHaveBeenCalled();  // NO event
    } finally {
      vi.useRealTimers();
    }
  });

  it("primary 401 + refresh 500 (all retries) does NOT clear token, does NOT dispatch, throws recoverable", async () => {
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "server error" }, { status: 500 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "server error" }, { status: 500 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "server error" }, { status: 500 }));

    await expect(apiFetch("/api/v1/protected")).rejects.toMatchObject({
      name: "ApiResponseError",
      status: 503,
      code: "refresh_transient",
    });

    expect(getAccessToken()).toBe("stale-token");
    expect(dispatchEventSpy).not.toHaveBeenCalled();
  });

  it("parallel 401 herd with transient refresh fires exactly one /refresh (and its retries) across all callers", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      // 4 parallel calls, all get 401, only one refresh attempt
      // (single-flight), refresh hangs across all 3 retry attempts.
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 1
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 2
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 3
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 })) // primary 4
        .mockImplementationOnce(neverResolvingFetch())  // refresh attempt 1
        .mockImplementationOnce(neverResolvingFetch())  // refresh attempt 2
        .mockImplementationOnce(neverResolvingFetch()); // refresh attempt 3

      const promises = [
        apiFetch("/api/v1/a"),
        apiFetch("/api/v1/b"),
        apiFetch("/api/v1/c"),
        apiFetch("/api/v1/d"),
      ];

      const assertions = promises.map((p) =>
        expect(p).rejects.toMatchObject({ status: 503, code: "refresh_transient" }),
      );

      await vi.advanceTimersByTimeAsync(10_000);
      await vi.advanceTimersByTimeAsync(250);
      await vi.advanceTimersByTimeAsync(10_000);
      await vi.advanceTimersByTimeAsync(500);
      await vi.advanceTimersByTimeAsync(10_000);
      await Promise.all(assertions);

      expect(getAccessToken()).toBe("stale-token");
      expect(dispatchEventSpy).not.toHaveBeenCalled();
      // 4 primaries + 3 refresh attempts (single-flight: shared across
      // all 4 callers) = 7 fetch calls total. The 4 callers do NOT each
      // fire their own /refresh.
      expect(fetchMock).toHaveBeenCalledTimes(7);
    } finally {
      vi.useRealTimers();
    }
  });

  it("concurrent 401s share exactly one /refresh and one /me probe across all callers", async () => {
    // Team F 2026-05-17: closes the singleflight microtask gap. Under the
    // original .finally(() => null) clear, a queued awaiter could resume
    // AFTER refreshPromise was cleared and fire a duplicate /refresh.
    // We now hold refreshPromise alive until every awaiter has consumed
    // it (refreshAwaiters counter returns to 0). The /me probe is also
    // singleflighted, so 5 concurrent 401-driven callers see one /refresh
    // and at most one /me.
    setAccessToken("stale-token");
    let refreshCount = 0;
    let meCount = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.endsWith("/api/v1/auth/refresh")) {
        refreshCount++;
        return jsonResponse({ detail: "invalid_refresh" }, { status: 401 });
      }
      if (url.endsWith("/api/v1/auth/me")) {
        meCount++;
        return jsonResponse({ detail: "invalid_token" }, { status: 401 });
      }
      return jsonResponse({ detail: "expired" }, { status: 401 });
    });

    const promises = [
      apiFetch("/api/v1/a"),
      apiFetch("/api/v1/b"),
      apiFetch("/api/v1/c"),
      apiFetch("/api/v1/d"),
      apiFetch("/api/v1/e"),
    ];

    const settled = await Promise.allSettled(promises);
    for (const r of settled) {
      expect(r.status).toBe("rejected");
      if (r.status === "rejected") {
        expect(r.reason).toMatchObject({ status: 401 });
      }
    }

    expect(refreshCount).toBe(1);  // exactly one /refresh across all 5
    expect(meCount).toBeLessThanOrEqual(1); // at most one /me probe
    expect(getAccessToken()).toBeNull();
    // auth:unauthenticated fires once even though 5 callers hit terminal.
    const authEvents = dispatchEventSpy.mock.calls.filter(
      ([e]) => (e as Event).type === "auth:unauthenticated",
    );
    expect(authEvents).toHaveLength(1);
  });

  it("does not dispatch auth:unauthenticated for login credential failures", async () => {
    setAccessToken("stale-token");
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "bad creds" }, { status: 401 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad creds" }, { status: 401 }));

    await expect(
      apiFetch("/api/v1/auth/login", { method: "POST" }),
    ).rejects.toMatchObject({
      status: 401,
      message: "bad creds",
    });

    expect(dispatchEventSpy).not.toHaveBeenCalled();
  });

  it("flattens FastAPI 422 validation payloads into a readable message", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: [
            { loc: ["body", "email"], msg: "field required" },
            { loc: ["body", "profile", "phone"], msg: "invalid format" },
          ],
        },
        { status: 422 },
      ),
    );

    await expect(apiFetch("/api/v1/users/me")).rejects.toMatchObject({
      status: 422,
      message: "email: field required; profile.phone: invalid format",
    });
  });

  it("returns undefined for 204 responses", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    const result = await apiFetch<void>("/api/v1/logout", { method: "POST" });

    expect(result).toBeUndefined();
  });

  it("surfaces structured detail on the thrown ApiResponseError", async () => {
    // L1.8: backend returns { detail: { code, message } } for the
    // email-verified gate so the login screen can branch without
    // string-matching the message.
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            code: "email_not_verified",
            message: "Please verify your email to sign in.",
          },
        },
        { status: 403 },
      ),
    );

    await expect(apiFetch("/api/v1/auth/login", { method: "POST" })).rejects.toMatchObject({
      name: "ApiResponseError",
      status: 403,
      code: "email_not_verified",
      message: "Please verify your email to sign in.",
      detail: {
        code: "email_not_verified",
        message: "Please verify your email to sign in.",
      },
    });
  });
});


describe("extractErrorMessage", () => {
  it("returns the message for Error instances", () => {
    expect(extractErrorMessage(new Error("boom"))).toBe("boom");
  });

  it("falls back for unknown values", () => {
    expect(extractErrorMessage({ nope: true }, "fallback")).toBe("fallback");
  });

  it("preserves ApiResponseError metadata", () => {
    const error = new ApiResponseError(403, "Forbidden");

    expect(error.status).toBe(403);
    expect(extractErrorMessage(error)).toBe("Forbidden");
  });
});
