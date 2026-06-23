/**
 * TDD tests for ``runSankeyQuery`` (api.ts) and ``useSankeyQuery`` hook.
 *
 * Mocking pattern mirrors the existing ``proactive-refresh.test.ts`` approach:
 * stub the global ``fetch`` via ``vi.stubGlobal`` so ``apiFetch`` (which
 * ``runSankeyQuery`` delegates to) posts to the real URL without hitting the
 * network.
 *
 * The hook tests use ``renderHook`` from ``@testing-library/react`` with an
 * SWR ``SWRConfig`` wrapper to disable deduplication/caching between cases.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { SWRConfig } from "swr";
import { setAccessToken } from "@/lib/api";
import { runSankeyQuery } from "@/lib/reports/api";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
import type { CanvasFilters, SankeyResponse, SankeyWidget } from "@/lib/reports/types";

// ─── helpers ────────────────────────────────────────────────────────────────

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const MOCK_SANKEY_RESPONSE: SankeyResponse = {
  links: [
    { source: "Income", target: "Groceries", value: 200 },
    { source: "Income", target: "Rent", value: 1000 },
  ],
  meta: { row_count: 2, truncated: false, query_ms: 12 },
};

/** A minimal SankeyWidget for tests. */
function makeWidget(overrides: Partial<SankeyWidget["config"]> = {}): SankeyWidget {
  return {
    id: "w-sankey-1",
    type: "sankey",
    title: "Cash flow",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      spending_granularity: "category",
      ...overrides,
    },
  };
}

/** SWR wrapper that disables deduplication so each test gets a fresh fetch. */
function swrWrapper({ children }: { children: React.ReactNode }) {
  return createElement(
    SWRConfig,
    { value: { provider: () => new Map(), dedupingInterval: 0 } },
    children,
  );
}

// ─── runSankeyQuery ──────────────────────────────────────────────────────────

describe("runSankeyQuery", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("test-bearer-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it("POSTs to /api/v1/reports/query/sankey with the supplied body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const result = await runSankeyQuery({
      filters: [],
      spending_granularity: "category",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/reports/query/sankey");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      filters: [],
      spending_granularity: "category",
    });
    expect(result.links).toHaveLength(2);
    expect(result.links[0]).toEqual({ source: "Income", target: "Groceries", value: 200 });
  });

  it("sends only backend-accepted keys — no dataset, measure, or extra fields", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    await runSankeyQuery({
      filters: [{ field: "date", op: "between", value: ["2026-01-01", "2026-01-31"] }],
      spending_granularity: "category_master",
      top_n: 10,
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    // These three are the ONLY accepted keys (SankeyQuery extra="forbid")
    expect(Object.keys(body).sort()).toEqual(["filters", "spending_granularity", "top_n"].sort());
  });

  it("omits top_n when not supplied", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    await runSankeyQuery({ filters: [], spending_granularity: "category" });

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body).not.toHaveProperty("top_n");
  });

  it("returns the parsed SankeyResponse with links and meta", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const result = await runSankeyQuery({ filters: [], spending_granularity: "category" });

    expect(result).toEqual(MOCK_SANKEY_RESPONSE);
    expect(result.meta.row_count).toBe(2);
    expect(result.meta.truncated).toBe(false);
  });
});

// ─── useSankeyQuery ──────────────────────────────────────────────────────────

describe("useSankeyQuery", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("test-bearer-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAccessToken(null);
  });

  it("calls runSankeyQuery and returns data once resolved", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const widget = makeWidget();
    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.data).toEqual(MOCK_SANKEY_RESPONSE);
    expect(result.current.error).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/reports/query/sankey");
  });

  it("resolves canvas date filter into the wire filters array", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const canvasFilters: CanvasFilters = {
      date_range: { start: "2026-01-01", end: "2026-01-31" },
    };
    const widget = makeWidget();

    const { result } = renderHook(
      () => useSankeyQuery(widget, canvasFilters),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body.filters).toContainEqual({
      field: "date",
      op: "between",
      value: ["2026-01-01", "2026-01-31"],
    });
  });

  it("widget-level date_range overrides the canvas date", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const canvasFilters: CanvasFilters = {
      date_range: { start: "2026-01-01", end: "2026-01-31" },
    };
    const widget = makeWidget({
      filters: { date_range: { start: "2026-02-01", end: "2026-02-28" } },
    });

    const { result } = renderHook(
      () => useSankeyQuery(widget, canvasFilters),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    // Widget date wins — canvas date must NOT appear
    expect(body.filters).toContainEqual({
      field: "date",
      op: "between",
      value: ["2026-02-01", "2026-02-28"],
    });
    expect(body.filters).not.toContainEqual(
      expect.objectContaining({ value: ["2026-01-01", "2026-01-31"] }),
    );
  });

  it("passes spending_granularity from widget config", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const widget = makeWidget({ spending_granularity: "category_master" });

    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body.spending_granularity).toBe("category_master");
  });

  it("passes top_n from widget config when set", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const widget = makeWidget({ top_n: 5 });

    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body.top_n).toBe(5);
  });

  it("omits top_n from wire body when not set in widget config", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const widget = makeWidget(); // no top_n

    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body).not.toHaveProperty("top_n");
  });

  it("does NOT send dataset or measure in the wire body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_SANKEY_RESPONSE));

    const widget = makeWidget();

    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
    expect(body).not.toHaveProperty("dataset");
    expect(body).not.toHaveProperty("measure");
  });

  it("surfaces error when the fetch fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Network error"));

    const widget = makeWidget();

    const { result } = renderHook(
      () => useSankeyQuery(widget, undefined),
      { wrapper: swrWrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.error).toBeDefined();
    expect(result.current.data).toBeUndefined();
  });
});
