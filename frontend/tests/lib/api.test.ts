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

  it("times out a hung refresh attempt and clears auth state", async () => {
    vi.useFakeTimers();
    try {
      setAccessToken("stale-token");
      fetchMock
        .mockResolvedValueOnce(jsonResponse({ detail: "expired" }, { status: 401 }))
        .mockImplementationOnce(neverResolvingFetch());

      const promise = apiFetch("/api/v1/protected");
      const assertion = expect(promise).rejects.toMatchObject({
        name: "ApiResponseError",
        status: 401,
        message: "expired",
      });

      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;
      expect(getAccessToken()).toBeNull();
      expect(dispatchEventSpy).toHaveBeenCalledWith(expect.any(Event));
      const event = dispatchEventSpy.mock.calls[0]?.[0];
      expect(event?.type).toBe("auth:unauthenticated");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears auth state and dispatches an event after terminal 401s", async () => {
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

    expect(getAccessToken()).toBeNull();
    expect(dispatchEventSpy).toHaveBeenCalledWith(expect.any(Event));
    const event = dispatchEventSpy.mock.calls[0]?.[0];
    expect(event?.type).toBe("auth:unauthenticated");
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
