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

// `getServerSession` is wrapped in React.cache() which memoizes per call site.
// To guarantee each test gets a fresh memoization, we re-import the module
// inside every test after `vi.resetModules()` in beforeEach.
async function loadModule() {
  const mod = await import("@/lib/auth-server");
  const loggerMod = await import("@/lib/logger");
  return { getServerSession: mod.getServerSession, logger: loggerMod.logger };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
});

describe("getServerSession", () => {
  it("returns null and does not fetch when no refresh cookie is present", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => undefined,
    });
    const fetchSpy = vi.spyOn(global, "fetch");
    const { getServerSession } = await loadModule();
    const session = await getServerSession();
    expect(session).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns null when fetch rejects, does not throw, logs sanitized warning", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "REDACTED-COOKIE-VALUE" }),
    });
    vi.spyOn(global, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch")
    );
    const { getServerSession, logger } = await loadModule();
    const session = await getServerSession();
    expect(session).toBeNull();
    // Post-PR-B: catch path emits `server_fetch_failed` from inside the
    // serverFetch helper instead of `server_session_verify_failed`.
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({
        error_name: "TypeError",
        error_message: expect.stringContaining("Failed to fetch"),
      })
    );
    // Critical privacy assertion: the logged payload must NOT contain the
    // cookie value, any token, or any header value.
    const logCallArgs = (logger.warn as unknown as ReturnType<typeof vi.fn>)
      .mock.calls[0][1];
    expect(JSON.stringify(logCallArgs)).not.toContain("REDACTED-COOKIE-VALUE");
  });

  it("returns null when res.json() throws on invalid JSON, does not throw", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    } as unknown as Response);
    const { getServerSession, logger } = await loadModule();
    const session = await getServerSession();
    expect(session).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      "server_fetch_failed",
      expect.objectContaining({ error_name: "SyntaxError" })
    );
  });

  it("returns null on non-OK response, does NOT log a warning (normal auth flow)", async () => {
    (cookies as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      get: () => ({ name: "refresh_token", value: "x" }),
    });
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "Invalid" }),
    } as unknown as Response);
    const { getServerSession, logger } = await loadModule();
    const session = await getServerSession();
    expect(session).toBeNull();
    // silentStatuses=[401] is passed by getServerSession for /auth/verify,
    // so the helper suppresses the warn for the 401 normal-flow case.
    // 500/503 would still emit `server_fetch_non_ok`.
    expect(logger.warn).not.toHaveBeenCalled();
  });

  it("returns the session payload on valid 200 response", async () => {
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
    const { getServerSession, logger } = await loadModule();
    const session = await getServerSession();
    expect(session).toEqual({
      user: expect.objectContaining({ id: 1, email: "a@b.c" }),
      accessToken: "TOK",
    });
    expect(logger.warn).not.toHaveBeenCalled();
  });
});
