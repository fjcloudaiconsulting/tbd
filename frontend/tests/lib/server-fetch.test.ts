import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the structured logger so we can assert the exact sanitized payload
// emitted on each failure branch, without writing to stdout/stderr in tests.
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// Stub server-only so the import works in a node/jsdom test env. Without
// this, `import "server-only"` throws at module load.
vi.mock("server-only", () => ({}));

async function loadModule() {
  const mod = await import("@/lib/server-fetch");
  const loggerMod = await import("@/lib/logger");
  return {
    serverFetch: mod.serverFetch,
    serverFetchResult: mod.serverFetchResult,
    logger: loggerMod.logger,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
});

describe("serverFetch", () => {
  it("returns null and logs sanitized warn when fetch rejects (no leaks)", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/probe", {
      method: "GET",
      cookie: "refresh_token=SECRET-COOKIE-VALUE",
      accessToken: "SECRET-BEARER-VALUE",
    });
    expect(result).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        method: "GET",
        path: "/api/v1/probe",
        error_name: "TypeError",
        error_message: expect.stringContaining("Failed to fetch"),
      }),
    );
    // backend_host is the host of the BACKEND URL, never user input.
    const logArgs = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(logArgs).toHaveProperty("backend_host");
    // Privacy invariant: no cookie, no bearer token, no header value in
    // the logged payload.
    const serialized = JSON.stringify(logArgs);
    expect(serialized).not.toContain("SECRET-COOKIE-VALUE");
    expect(serialized).not.toContain("SECRET-BEARER-VALUE");
    expect(serialized).not.toContain("Bearer ");
  });

  it("returns null and logs sanitized warn when res.json() throws on invalid JSON", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/probe", {
      accessToken: "SECRET-BEARER-VALUE",
    });
    expect(result).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        path: "/api/v1/probe",
        error_name: "SyntaxError",
      }),
    );
    const logArgs = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(JSON.stringify(logArgs)).not.toContain("SECRET-BEARER-VALUE");
  });

  it("returns null and emits server_fetch_non_ok warn on a non-OK response by default", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Service Unavailable" }),
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/probe", {
      method: "POST",
      accessToken: "SECRET-BEARER-VALUE",
    });
    expect(result).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_non_ok",
      expect.objectContaining({
        method: "POST",
        path: "/api/v1/probe",
        status: 503,
      }),
    );
    const logArgs = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(JSON.stringify(logArgs)).not.toContain("SECRET-BEARER-VALUE");
  });

  it("silentStatuses suppresses listed status codes only", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Unauthorized" }),
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/auth/verify", {
      method: "POST",
      cookie: "refresh_token=x",
      silentStatuses: [401],
    });
    expect(result).toBeNull();
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("silentStatuses does NOT suppress non-listed status codes (outage signal preserved)", async () => {
    // 503 is a real backend outage; it must still warn even when the call
    // site has silenced its expected normal-flow status (401).
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Service Unavailable" }),
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/auth/verify", {
      method: "POST",
      cookie: "refresh_token=x",
      silentStatuses: [401],
    });
    expect(result).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_non_ok",
      expect.objectContaining({ status: 503 }),
    );
  });

  it("server_fetch_failed event strips query string from path", async () => {
    // Defense-in-depth for PR #283's query-stripping policy: even though
    // the helper documents that callers must not put secrets in the path,
    // the failure logger strips any query string before logging.
    vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const { serverFetch, logger } = await loadModule();
    await serverFetch<{ ok: boolean }>(
      "/api/v1/auth/something?token=SECRET-QUERY-VALUE",
      {},
    );
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        path: "/api/v1/auth/something",
      }),
    );
    const payload = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(JSON.stringify(payload)).not.toContain("SECRET-QUERY-VALUE");
  });

  it("server_fetch_non_ok event strips query string from path", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Service Unavailable" }),
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    await serverFetch<{ ok: boolean }>(
      "/api/v1/something?reset_token=SECRET-RESET-VALUE",
      {},
    );
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_non_ok",
      expect.objectContaining({
        path: "/api/v1/something",
        status: 503,
      }),
    );
    const payload = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(JSON.stringify(payload)).not.toContain("SECRET-RESET-VALUE");
  });

  it("returns parsed JSON and does not warn on a 200 response", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ value: 42 }),
    } as unknown as Response);
    const { serverFetch, logger } = await loadModule();
    const result = await serverFetch<{ value: number }>("/api/v1/probe", {
      accessToken: "SECRET-BEARER-VALUE",
    });
    expect(result).toEqual({ value: 42 });
    expect(logger.warn).not.toHaveBeenCalled();
  });
});

describe("safeBackendHost", () => {
  // SERVER_API_URL is resolved once at module load from process.env, so
  // these tests use vi.stubEnv + vi.resetModules + a fresh dynamic
  // import to exercise the exported helper under different env values.
  // This replaces a previous test that exercised a local `guarded()`
  // function rather than the actual exported helper.

  it("safeBackendHost returns 'invalid-backend-url' when BACKEND_INTERNAL_URL is malformed", async () => {
    vi.stubEnv("BACKEND_INTERNAL_URL", "not a url");
    vi.resetModules();
    const mod = await import("@/lib/server-fetch");
    expect(mod.safeBackendHost()).toBe("invalid-backend-url");
    vi.unstubAllEnvs();
  });

  it("safeBackendHost returns parsed host when BACKEND_INTERNAL_URL is well-formed", async () => {
    vi.stubEnv("BACKEND_INTERNAL_URL", "https://backend:8000");
    vi.resetModules();
    const mod = await import("@/lib/server-fetch");
    expect(mod.safeBackendHost()).toBe("backend:8000");
    vi.unstubAllEnvs();
  });
});

describe("serverFetch timeout (PR #288)", () => {
  it("aborts a never-resolving fetch within the configured timeoutMs and returns null", async () => {
    // The fetch promise never resolves. Without an AbortController the
    // helper would hang forever; with PR #288's timeout it must abort
    // and return null. We use a tight 50ms budget so the test stays
    // fast.
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
          // No resolve path — fetch hangs unless aborted.
        }),
    );
    const { serverFetch, logger } = await loadModule();
    const t0 = Date.now();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/slow", {
      timeoutMs: 50,
    });
    const elapsed = Date.now() - t0;
    expect(result).toBeNull();
    // We can't be super tight on the upper bound in CI, but a 5000ms
    // hang would dwarf this — assert the helper aborted well before
    // the next test's default budget.
    expect(elapsed).toBeLessThan(2_000);
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        reason: "timeout",
        timeout_ms: 50,
        path: "/api/v1/slow",
      }),
    );
  });

  it("serverFetchResult returns kind=timeout on abort and does not leak query string or token", async () => {
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
    const { serverFetchResult, logger } = await loadModule();
    const result = await serverFetchResult<{ ok: boolean }>(
      "/api/v1/slow?leak_token=SECRET-QUERY-VALUE",
      {
        timeoutMs: 30,
        accessToken: "SECRET-BEARER-VALUE",
        cookie: "refresh_token=SECRET-COOKIE-VALUE",
      },
    );
    expect(result).toEqual({ kind: "timeout" });
    const payload = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    expect(payload.reason).toBe("timeout");
    expect(payload.path).toBe("/api/v1/slow");
    const serialized = JSON.stringify(payload);
    expect(serialized).not.toContain("SECRET-QUERY-VALUE");
    expect(serialized).not.toContain("SECRET-BEARER-VALUE");
    expect(serialized).not.toContain("SECRET-COOKIE-VALUE");
    expect(serialized).not.toContain("Bearer ");
  });

  it("timeoutMs override actually changes the budget (50ms aborts a 200ms-delayed promise)", async () => {
    vi.spyOn(global, "fetch").mockImplementation(
      (_input, init) =>
        new Promise<Response>((resolve, reject) => {
          const signal = (init as RequestInit | undefined)?.signal;
          const t = setTimeout(() => {
            resolve({
              ok: true,
              json: async () => ({ ok: true }),
            } as unknown as Response);
          }, 200);
          if (signal) {
            const onAbort = () => {
              clearTimeout(t);
              const err = new Error("Aborted");
              err.name = "AbortError";
              reject(err);
            };
            if (signal.aborted) onAbort();
            else signal.addEventListener("abort", onAbort, { once: true });
          }
        }),
    );
    const { serverFetch } = await loadModule();
    const t0 = Date.now();
    const result = await serverFetch<{ ok: boolean }>("/api/v1/slow", {
      timeoutMs: 50,
    });
    const elapsed = Date.now() - t0;
    expect(result).toBeNull();
    // Aborted before the 200ms delay would have resolved.
    expect(elapsed).toBeLessThan(180);
  });

  it("default timeout is 5000ms when no override is supplied", async () => {
    // Spy on setTimeout to confirm the helper schedules the abort at the
    // documented default. This avoids actually waiting 5s in the test.
    const setTimeoutSpy = vi
      .spyOn(global, "setTimeout")
      .mockImplementation(
        // Return a fake handle; the test doesn't await the resolution,
        // it just inspects how the helper armed the timer.
        ((..._args: unknown[]) => 0) as unknown as typeof global.setTimeout,
      );
    // fetch resolves immediately so the helper unwinds cleanly through
    // the finally{ clearTimeout } branch.
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    } as unknown as Response);
    const { serverFetch } = await loadModule();
    await serverFetch<{ ok: boolean }>("/api/v1/probe", {});
    // The first setTimeout call inside serverFetchResult schedules the
    // abort with the default budget.
    const firstCall = setTimeoutSpy.mock.calls[0];
    expect(firstCall).toBeDefined();
    expect(firstCall[1]).toBe(5000);
    setTimeoutSpy.mockRestore();
  });

  it("clearTimeout is invoked on the success path so the abort never fires after the response lands", async () => {
    const clearTimeoutSpy = vi.spyOn(global, "clearTimeout");
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ value: 1 }),
    } as unknown as Response);
    const { serverFetch } = await loadModule();
    const result = await serverFetch<{ value: number }>("/api/v1/probe", {});
    expect(result).toEqual({ value: 1 });
    expect(clearTimeoutSpy).toHaveBeenCalled();
    clearTimeoutSpy.mockRestore();
  });
});

describe("serverFetchResult discriminated kinds (PR #288)", () => {
  it("returns kind=ok with parsed JSON on 200", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ value: 42 }),
    } as unknown as Response);
    const { serverFetchResult } = await loadModule();
    const result = await serverFetchResult<{ value: number }>("/api/v1/probe");
    expect(result).toEqual({ kind: "ok", data: { value: 42 } });
  });

  it("returns kind=http_error with status on 401", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Unauthorized" }),
    } as unknown as Response);
    const { serverFetchResult } = await loadModule();
    const result = await serverFetchResult<{ ok: boolean }>(
      "/api/v1/auth/verify",
      { silentStatuses: [401] },
    );
    expect(result).toEqual({ kind: "http_error", status: 401 });
  });

  it("returns kind=http_error with status on 503", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Unavailable" }),
    } as unknown as Response);
    const { serverFetchResult } = await loadModule();
    const result = await serverFetchResult<{ ok: boolean }>("/api/v1/probe");
    expect(result).toEqual({ kind: "http_error", status: 503 });
  });

  it("returns kind=network_error on a TypeError from fetch", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const { serverFetchResult } = await loadModule();
    const result = await serverFetchResult<{ ok: boolean }>("/api/v1/probe");
    expect(result).toEqual({ kind: "network_error" });
  });

  it("returns kind=invalid_json when res.json() throws on 200", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    } as unknown as Response);
    const { serverFetchResult } = await loadModule();
    const result = await serverFetchResult<{ ok: boolean }>("/api/v1/probe");
    expect(result).toEqual({ kind: "invalid_json" });
  });
});

describe("serverFetch privacy regression (end-to-end)", () => {
  // Combine the two threats: malformed BACKEND_INTERNAL_URL + a path
  // with a secret query string. Node's fetch throws errors whose
  // .message can include the full attempted URL (with query) e.g.
  // 'Failed to parse URL from not a url/api?token=SECRET_QUERY'. The
  // sanitizeErrorMessage helper must scrub the secret before logging.

  it("serverFetch failure with malformed BACKEND_INTERNAL_URL never logs secrets from path or error", async () => {
    vi.stubEnv("BACKEND_INTERNAL_URL", "not a url");
    vi.resetModules();
    // Reproduce the exact error shape Node's fetch throws when the
    // assembled URL is unparseable. In jsdom test env, fetch doesn't
    // necessarily reject on its own for a malformed URL string, so we
    // mock it to throw the verified architect shape:
    //   'Failed to parse URL from not a url/api?token=SECRET_QUERY'
    vi.spyOn(global, "fetch").mockRejectedValue(
      new TypeError(
        "Failed to parse URL from not a url/api/v1/auth/something?token=SECRET-QUERY-VALUE",
      ),
    );
    // The vi.mock("@/lib/logger", ...) declaration at the top of this
    // file is hoisted and survives resetModules — the freshly-imported
    // module graph still resolves to the same mocked logger instance.
    const mod = await import("@/lib/server-fetch");
    const { logger } = await import("@/lib/logger");
    (logger.warn as unknown as ReturnType<typeof vi.fn>).mockClear?.();

    await mod.serverFetch(
      "/api/v1/auth/something?token=SECRET-QUERY-VALUE",
      {},
    );

    expect(logger.warn).toHaveBeenCalled();
    const payload = (logger.warn as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0][1];
    const serialized = JSON.stringify(payload);
    expect(serialized).not.toContain("SECRET-QUERY-VALUE");
    // Sanity-check: backend_host fell back to the sentinel, proving the
    // malformed-env path was the one exercised.
    expect(payload.backend_host).toBe("invalid-backend-url");

    vi.unstubAllEnvs();
  });
});
