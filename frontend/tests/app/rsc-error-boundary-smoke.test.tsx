import { describe, it, expect, vi, beforeEach } from "vitest";

// Fault-injection smoke test against the #282 / #288 class of bug.
//
// Two correctness properties:
//   1. Unauthenticated (401/403 from /auth/verify) → redirect to /login.
//      The Next.js error boundary must not be reached.
//   2. Transient (timeout / 5xx / network error / invalid payload from
//      /auth/verify) → DO NOT redirect. Render a recoverable client
//      island with empty fallback data so the client-side SWR layer
//      can re-fetch after hydration. This is the #288 fix: pre-PR,
//      any serverFetch failure on the verify path either hung the
//      render or false-logged-out the user.

// Stub server-only so module imports work outside a real server context.
vi.mock("server-only", () => ({}));

// Mock both flavors of the helper. The page-level data reads use
// `serverFetch` (T | null); the verify path inside `getServerSessionResult`
// uses `serverFetchResult` (the discriminated form). Tests below stub
// each as needed.
const serverFetchMock = vi.fn(async () => null);
const serverFetchResultMock = vi.fn(async () => ({ kind: "ok", data: {} }));
vi.mock("@/lib/server-fetch", () => ({
  serverFetch: serverFetchMock,
  serverFetchResult: serverFetchResultMock,
}));

// Mock the logger to keep test output quiet and isolated.
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// Mock next/headers — getServerSessionResult reads cookies; we provide one
// so the verify path is exercised in every test.
vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: () => ({ name: "refresh_token", value: "x" }),
  }),
}));

// Mock next/navigation's redirect so we can assert it was called. We
// throw with NEXT_REDIRECT to match the real Next.js behavior, which is
// what short-circuits RSC render before any error boundary catches it.
const redirectMock = vi.fn((_target: string) => {
  throw new Error("NEXT_REDIRECT");
});
vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
  redirectMock.mockImplementation((_target: string) => {
    throw new Error("NEXT_REDIRECT");
  });
});

describe("RSC error-boundary smoke — unauthenticated redirects to /login", () => {
  it("/forecast-plans: 401 on /auth/verify → redirect('/login')", async () => {
    serverFetchResultMock.mockResolvedValue({
      kind: "http_error",
      status: 401,
    } as never);
    const mod = await import("@/app/forecast-plans/page");
    const ForecastPlansPage = mod.default;
    await expect(ForecastPlansPage()).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/login");
  });

  it("/import/[id]/reconcile: 403 on /auth/verify → redirect('/login')", async () => {
    serverFetchResultMock.mockResolvedValue({
      kind: "http_error",
      status: 403,
    } as never);
    const mod = await import("@/app/import/[import_id]/reconcile/page");
    const ReconcilePage = mod.default;
    await expect(
      ReconcilePage({ params: Promise.resolve({ import_id: "1" }) }),
    ).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/login");
  });
});

describe("RSC error-boundary smoke — transient verify does NOT redirect (PR #288)", () => {
  it("/forecast-plans: 503 on /auth/verify → no redirect, renders client island", async () => {
    serverFetchResultMock.mockResolvedValue({
      kind: "http_error",
      status: 503,
    } as never);
    const mod = await import("@/app/forecast-plans/page");
    const ForecastPlansPage = mod.default;
    // Must resolve to a React element rather than throwing NEXT_REDIRECT.
    const out = await ForecastPlansPage();
    expect(out).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalledWith("/login");
  });

  it("/forecast-plans: timeout on /auth/verify → no redirect, renders client island", async () => {
    serverFetchResultMock.mockResolvedValue({ kind: "timeout" } as never);
    const mod = await import("@/app/forecast-plans/page");
    const ForecastPlansPage = mod.default;
    const out = await ForecastPlansPage();
    expect(out).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalledWith("/login");
  });

  it("/forecast-plans: network_error on /auth/verify → no redirect, renders client island", async () => {
    serverFetchResultMock.mockResolvedValue({
      kind: "network_error",
    } as never);
    const mod = await import("@/app/forecast-plans/page");
    const ForecastPlansPage = mod.default;
    const out = await ForecastPlansPage();
    expect(out).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalledWith("/login");
  });

  it("/import/[id]/reconcile: 500 on /auth/verify → no redirect, renders client island", async () => {
    serverFetchResultMock.mockResolvedValue({
      kind: "http_error",
      status: 500,
    } as never);
    const mod = await import("@/app/import/[import_id]/reconcile/page");
    const ReconcilePage = mod.default;
    const out = await ReconcilePage({
      params: Promise.resolve({ import_id: "1" }),
    });
    expect(out).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalledWith("/login");
  });
});
