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
