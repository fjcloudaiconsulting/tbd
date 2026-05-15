import { describe, it, expect, vi, beforeEach } from "vitest";

// RSC-level tests for `app/forecast-plans/page.tsx` (PR #288).
//
// The page is an async Server Component. It calls
// `getServerSessionResult()` and switches on the three result kinds:
//
//   - "unauthenticated" → `redirect('/login')`.
//   - "transient"       → render the client island with empty fallback
//     data (no redirect, no throw, no spinner-forever).
//   - "authenticated"   → fetch initial categories/periods/plan via
//     `serverFetch` and hand them down to the client.
//
// We assert by inspecting the React element returned by the page. JSX
// in an RSC is lazy: `<ForecastPlansClient ... />` produces a React
// element object whose `.props` carry the fallback data. The element
// is not invoked unless something downstream renders it (Next.js does
// this in production; in test, inspecting `.props` is sufficient).

vi.mock("server-only", () => ({}));

// Mock the auth-server module directly. We're testing the page's
// orchestration logic, not the auth-server triage logic (which has its
// own test file under tests/lib/auth-server.test.ts).
const getServerSessionResultMock = vi.fn();
vi.mock("@/lib/auth-server", () => ({
  getServerSessionResult: getServerSessionResultMock,
}));

const serverFetchMock = vi.fn(async () => null);
vi.mock("@/lib/server-fetch", () => ({
  serverFetch: serverFetchMock,
  serverFetchResult: vi.fn(),
}));

vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const redirectMock = vi.fn((_target: string) => {
  throw new Error("NEXT_REDIRECT");
});
vi.mock("next/navigation", () => ({
  redirect: redirectMock,
}));

// Quietly stub the client island so the page's JSX import resolves
// without dragging in recharts / SWR.
vi.mock("@/app/forecast-plans/ForecastPlansClient", () => ({
  __esModule: true,
  default: () => null,
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
  redirectMock.mockImplementation((_target: string) => {
    throw new Error("NEXT_REDIRECT");
  });
});

describe("ForecastPlansPage RSC orchestration (PR #288)", () => {
  it("unauthenticated session → redirect('/login') and no data fetches", async () => {
    getServerSessionResultMock.mockResolvedValue({ kind: "unauthenticated" });
    const { default: ForecastPlansPage } = await import(
      "@/app/forecast-plans/page"
    );
    await expect(ForecastPlansPage()).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/login");
    expect(serverFetchMock).not.toHaveBeenCalled();
  });

  it("transient session → renders client island with empty fallback data, NO redirect, NO data fetches", async () => {
    getServerSessionResultMock.mockResolvedValue({
      kind: "transient",
      reason: "timeout",
    });
    const { default: ForecastPlansPage } = await import(
      "@/app/forecast-plans/page"
    );
    const element = await ForecastPlansPage();
    expect(element).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalledWith("/login");
    expect(serverFetchMock).not.toHaveBeenCalled();
    // React element props expose the initial fallback shape.
    const props = (element as { props: Record<string, unknown> }).props;
    expect(props.initialPeriods).toEqual([]);
    expect(props.initialCategories).toEqual([]);
    expect(props.initialPlan).toBeNull();
  });

  it("authenticated session → fetches categories + periods + plan, no redirect", async () => {
    getServerSessionResultMock.mockResolvedValue({
      kind: "authenticated",
      session: { user: { id: 1 }, accessToken: "TOK" },
    });
    serverFetchMock.mockResolvedValueOnce([
      { id: 10, name: "Salary" },
    ] as never);
    serverFetchMock.mockResolvedValueOnce([
      { id: 1, start_date: "2026-05-01", end_date: null },
    ] as never);
    serverFetchMock.mockResolvedValueOnce({ id: 100, items: [] } as never);
    const { default: ForecastPlansPage } = await import(
      "@/app/forecast-plans/page"
    );
    const element = await ForecastPlansPage();
    expect(element).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalled();
    const props = (element as { props: Record<string, unknown> }).props;
    expect(props.initialCategories).toEqual([{ id: 10, name: "Salary" }]);
    expect(props.initialPeriods).toEqual([
      { id: 1, start_date: "2026-05-01", end_date: null },
    ]);
    expect(props.initialPlan).toEqual({ id: 100, items: [] });
  });

  it("authenticated session with one fetch rejecting → uses Promise.allSettled to recover, renders with partial data", async () => {
    getServerSessionResultMock.mockResolvedValue({
      kind: "authenticated",
      session: { user: { id: 1 }, accessToken: "TOK" },
    });
    // categories rejects (Promise.all would have re-thrown; Promise.allSettled
    // lets us swallow it and pass [] to the client).
    serverFetchMock.mockRejectedValueOnce(new Error("Boom"));
    // billing-periods OK
    serverFetchMock.mockResolvedValueOnce([
      { id: 1, start_date: "2026-05-01", end_date: null },
    ] as never);
    // plan OK
    serverFetchMock.mockResolvedValueOnce({ id: 100, items: [] } as never);
    const { default: ForecastPlansPage } = await import(
      "@/app/forecast-plans/page"
    );
    const element = await ForecastPlansPage();
    expect(element).toBeTruthy();
    expect(redirectMock).not.toHaveBeenCalled();
    const props = (element as { props: Record<string, unknown> }).props;
    expect(props.initialCategories).toEqual([]);
    expect(props.initialPeriods).toEqual([
      { id: 1, start_date: "2026-05-01", end_date: null },
    ]);
  });
});
