import { describe, it, expect, vi, beforeEach } from "vitest";

// TBD-624 F2/F3. The server must not fetch as the shared refresh cookie's
// identity, so these pages render the client island on empty seeds and make no
// request. next/headers is mocked WITH a cookie and fetch with a valid verify
// answer, so a page that still reads the session would visibly fetch.
vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({
  cookies: vi.fn(async () => ({
    get: () => ({ name: "refresh_token", value: "COOKIE-OF-OTHER-TAB" }),
  })),
}));
vi.mock("@/lib/logger", () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const redirectMock = vi.hoisted(() =>
  vi.fn((_t: string) => {
    throw new Error("NEXT_REDIRECT");
  }),
);
vi.mock("next/navigation", () => ({ redirect: redirectMock }));

vi.mock("@/app/forecast-plans/ForecastPlansClient", () => ({ __esModule: true, default: () => null }));
vi.mock("@/app/import/[import_id]/reconcile/ReconcileClient", () => ({ __esModule: true, default: () => null }));

const fetchSpy = vi.fn(async () =>
  new Response(
    JSON.stringify({ user: { id: 2 }, access_token: "TOK-B", token_type: "bearer" }),
    { status: 200, headers: { "content-type": "application/json" } },
  ),
);

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", fetchSpy);
});

function noServerRequests() {
  expect(fetchSpy).not.toHaveBeenCalled();
}

describe("TBD-624 F2: ForecastPlansPage", () => {
  it("makes zero fetches and seeds the island empty", async () => {
    // Kills: keeping 'just the plan read' (get_or_create = a cross-org write).
    const { default: Page } = await import("@/app/forecast-plans/page");
    const el = (await Promise.resolve(Page())) as { props: Record<string, unknown> };
    noServerRequests();
    expect(el.props.initialPlan).toBeNull();
    expect(el.props.initialPeriods).toEqual([]);
    expect(el.props.initialCategories).toEqual([]);
    expect(redirectMock).not.toHaveBeenCalled();
  });
});

describe("TBD-624 F3: ReconcilePage", () => {
  const load = () => import("@/app/import/[import_id]/reconcile/page");

  it("a valid id renders the island with initialBatch=null and no fetch", async () => {
    // Kills: keeping the batch seed.
    const { default: Page } = await load();
    const el = (await Page({ params: Promise.resolve({ import_id: "7" }) })) as { props: Record<string, unknown> };
    noServerRequests();
    expect(el.props.batchId).toBe(7);
    expect(el.props.initialBatch).toBeNull();
  });

  it.each(["abc", "0", "-3"])("invalid id %s still redirects to /import", async (id) => {
    // Kills: dropping the id guard along with the session code.
    const { default: Page } = await load();
    await expect(Page({ params: Promise.resolve({ import_id: id }) })).rejects.toThrow("NEXT_REDIRECT");
    expect(redirectMock).toHaveBeenCalledWith("/import");
    noServerRequests();
  });
});
