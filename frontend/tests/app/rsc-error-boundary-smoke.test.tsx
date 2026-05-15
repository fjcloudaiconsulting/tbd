import { describe, it, expect, vi, beforeEach } from "vitest";

// Fault-injection smoke test against the #282 class of bug.
//
// When serverFetch returns null (simulating transient backend
// unavailability) during RSC render, the page MUST redirect to /login
// rather than surface as the Next.js error boundary. We assert this by
// mocking serverFetch to return null, and asserting the page invokes
// `next/navigation`'s `redirect("/login")`, which throws NEXT_REDIRECT
// — Next.js's documented short-circuit mechanism.

// Stub server-only so module imports work outside a real server context.
vi.mock("server-only", () => ({}));

// Mock serverFetch to return null unconditionally — simulates a backend
// outage where /auth/verify rejects or returns non-OK.
vi.mock("@/lib/server-fetch", () => ({
  serverFetch: vi.fn(async () => null),
}));

// Mock the logger to keep test output quiet and isolated.
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// Mock next/headers — getServerSession reads cookies; without a refresh
// cookie it returns null before serverFetch is reached. We provide one so
// the path under test (serverFetch returns null → redirect) is exercised.
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
  redirectMock.mockImplementation((_target: string) => {
    throw new Error("NEXT_REDIRECT");
  });
});

describe("RSC error-boundary smoke", () => {
  it("/forecast-plans redirects to /login when serverFetch returns null (no error boundary)", async () => {
    const mod = await import("@/app/forecast-plans/page");
    const ForecastPlansPage = mod.default;
    await expect(ForecastPlansPage()).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/login");
  });

  // /import/[import_id]/reconcile uses the same getServerSession →
  // serverFetch → redirect("/login") path. We exercise it here with a
  // synthetic params promise to cover the second server-surface caller.
  it("/import/[import_id]/reconcile redirects to /login when serverFetch returns null (no error boundary)", async () => {
    const mod = await import("@/app/import/[import_id]/reconcile/page");
    const ReconcilePage = mod.default;
    await expect(
      ReconcilePage({ params: Promise.resolve({ import_id: "1" }) }),
    ).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/login");
  });
});
