import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock next/headers so the module under test doesn't blow up on the
// server-only `cookies()` import outside a real Next.js request.
vi.mock("next/headers", () => ({
  cookies: vi.fn(),
}));

// Mock the structured logger so we can assert the exact sanitized payload
// emitted on the catch branch, without writing to stdout/stderr in tests.
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// Stub server-only so the import works in a node/jsdom test env. Without
// this, `import "server-only"` throws at module load.
vi.mock("server-only", () => ({}));

import { cookies } from "next/headers";

// `getServerSessionResult` is wrapped in React.cache() which memoizes per
// call site. To guarantee each test gets a fresh memoization, we re-import
// the module inside every test after `vi.resetModules()` in beforeEach.
async function loadModule() {
  const mod = await import("@/lib/auth-server");
  const loggerMod = await import("@/lib/logger");
  return {
    getServerSessionResult: mod.getServerSessionResult,
    logger: loggerMod.logger,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
});

describe("getServerSessionResult", () => {
  it("returns kind=unauthenticated when no refresh cookie is present, does not fetch", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => undefined,
    });
    const fetchSpy = vi.spyOn(global, "fetch");
    const { getServerSessionResult } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "unauthenticated" });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns kind=transient(network) when fetch rejects, does not throw, logs sanitized warning", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "REDACTED-COOKIE-VALUE" }),
    });
    vi.spyOn(global, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const { getServerSessionResult, logger } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "transient", reason: "network" });
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        error_name: "TypeError",
        error_message: expect.stringContaining("Failed to fetch"),
        reason: "network_error",
      }),
    );
    // Critical privacy assertion: the logged payload must NOT contain the
    // cookie value, any token, or any header value.
    const logCallArgs = (logger.warn as unknown as ReturnType<typeof vi.fn>)
      .mock.calls[0][1];
    expect(JSON.stringify(logCallArgs)).not.toContain("REDACTED-COOKIE-VALUE");
  });

  it("returns kind=transient(invalid_payload) when res.json() throws on 200", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    } as unknown as Response);
    const { getServerSessionResult, logger } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "transient", reason: "invalid_payload" });
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({ error_name: "SyntaxError" }),
    );
  });

  it("returns kind=transient(invalid_payload) when 200 response is missing access_token or user", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      // Payload shape that 2xx-parses but doesn't carry the required
      // session fields. Pre-PR-#288 this collapsed to "null" which the
      // page mapped to /login → false-logout on a server contract drift.
      json: async () => ({ token_type: "bearer" }),
    } as unknown as Response);
    const { getServerSessionResult } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "transient", reason: "invalid_payload" });
  });

  it("returns kind=unauthenticated on 401 (normal auth flow, no logged warning)", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Invalid" }),
    } as unknown as Response);
    const { getServerSessionResult, logger } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "unauthenticated" });
    // silentStatuses=[401] is passed by getServerSession for /auth/verify,
    // so the helper suppresses the warn for the 401 normal-flow case.
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("returns kind=unauthenticated on 403", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ detail: "Forbidden" }),
    } as unknown as Response);
    const { getServerSessionResult } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "unauthenticated" });
  });

  it("returns kind=transient(server_error) on 500 (does NOT redirect to /login)", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: "Internal" }),
    } as unknown as Response);
    const { getServerSessionResult, logger } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "transient", reason: "server_error" });
    // 500 is NOT in silentStatuses; the non-OK warn must fire so on-call
    // sees the backend outage signal.
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_non_ok",
      expect.objectContaining({ status: 500 }),
    );
  });

  it("returns kind=transient(server_error) on 503", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Unavailable" }),
    } as unknown as Response);
    const { getServerSessionResult } = await loadModule();
    const result = await getServerSessionResult();
    expect(result).toEqual({ kind: "transient", reason: "server_error" });
  });

  it("returns kind=transient(timeout) when /auth/verify never responds within the budget (no hang)", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    // Simulate a wedged backend: fetch hangs until aborted.
    vi.spyOn(global, "fetch").mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = (init as RequestInit | undefined)?.signal;
          if (signal) {
            const onAbort = () => {
              const err = new Error("Aborted");
              err.name = "AbortError";
              reject(err);
            };
            if (signal.aborted) onAbort();
            else signal.addEventListener("abort", onAbort, { once: true });
          }
        }),
    );
    // Stub the verify timeout to be tight so the test doesn't burn 5s.
    // We do this by relying on the default 5s being abortable; instead
    // of swapping it out, we use a short race against a node-level
    // timer in the test runner to validate "no hang" rather than the
    // exact budget.
    const { getServerSessionResult } = await loadModule();
    // Race the session result against a 2s timer. The default verify
    // budget is 5s, but our mock fetch will be aborted by the helper's
    // AbortController as soon as the budget elapses; we only need to
    // confirm the test doesn't hang. To keep this fast, we instead
    // stub `setTimeout` so the helper's abort fires immediately.
    // The simpler shape: assert the helper returns transient(timeout)
    // when the budget is short. Re-import with a custom budget by
    // mocking the constant indirectly is overkill; we run the actual
    // 5s timer here but with Vitest's fake timers.
    vi.useFakeTimers();
    const promise = getServerSessionResult();
    // Advance past the 5s verify budget.
    await vi.advanceTimersByTimeAsync(5_001);
    const result = await promise;
    vi.useRealTimers();
    expect(result).toEqual({ kind: "transient", reason: "timeout" });
  });

  it("returns kind=authenticated on a valid 200 response", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({
        user: {
          id: 1,
          username: "alice",
          email: "a@b.c",
          first_name: null,
          last_name: null,
          phone: null,
          avatar_url: null,
          email_verified: true,
          role: "owner",
          org_id: 1,
          org_name: "Acme",
          billing_cycle_day: 1,
          is_superadmin: false,
          is_active: true,
          mfa_enabled: false,
          password_set: true,
        },
        access_token: "TOK",
        token_type: "bearer",
      }),
    } as unknown as Response);
    const { getServerSessionResult, logger } = await loadModule();
    const result = await getServerSessionResult();
    expect(result.kind).toBe("authenticated");
    if (result.kind === "authenticated") {
      expect(result.session.accessToken).toBe("TOK");
      expect(result.session.user.id).toBe(1);
      expect(result.session.user.email).toBe("a@b.c");
    }
    expect(logger.warn).not.toHaveBeenCalled();
  });
});
