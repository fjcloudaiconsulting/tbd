/**
 * TDD tests for ``getDashboard`` and ``saveDashboard`` (lib/dashboard/api.ts).
 *
 * Mocking pattern mirrors the ``use-sankey-query.test.ts`` approach:
 * stub global ``fetch`` via ``vi.stubGlobal`` so ``apiFetch`` (which
 * the dashboard functions delegate to) hits the real URL path without
 * touching the network.
 */

import { setAccessToken } from "@/lib/api";
import { getDashboard, saveDashboard } from "@/lib/dashboard/api";
import type { DashboardLayoutResponse } from "@/lib/dashboard/types";

// ─── helpers ────────────────────────────────────────────────────────────────

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const MOCK_LAYOUT: DashboardLayoutResponse = {
  id: 1,
  owner_user_id: 42,
  org_id: 7,
  layout_json: { version: 1, widgets: [] },
  canvas_filters_json: {},
  schema_version: 1,
  created_at: "2026-06-24T10:00:00Z",
  updated_at: "2026-06-24T10:00:00Z",
};

// ─── getDashboard ────────────────────────────────────────────────────────────

describe("getDashboard", () => {
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

  it("GETs /api/v1/dashboard", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    await getDashboard();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/dashboard");
    // Default method for a GET is undefined or "GET"
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("returns the parsed DashboardLayoutResponse", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    const result = await getDashboard();

    expect(result).toEqual(MOCK_LAYOUT);
    expect(result.id).toBe(1);
    expect(result.owner_user_id).toBe(42);
    expect(result.org_id).toBe(7);
    expect(result.schema_version).toBe(1);
  });

  it("includes the Bearer token in the Authorization header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    await getDashboard();

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers as HeadersInit);
    expect(headers.get("authorization")).toBe("Bearer test-bearer-token");
  });
});

// ─── saveDashboard ───────────────────────────────────────────────────────────

describe("saveDashboard", () => {
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

  it("PATCHes /api/v1/dashboard", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    await saveDashboard(
      { version: 1, widgets: [] },
      {},
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/dashboard");
    expect(init?.method).toBe("PATCH");
  });

  it("sends only layout_json and canvas_filters_json in the body (extra=forbid guard)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    const layout = { version: 1 as const, widgets: [] };
    const filters = { date_range: { start: "2026-01-01", end: "2026-01-31" } };

    await saveDashboard(layout, filters);

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init?.body as string);
    // Exactly these two keys — backend uses extra="forbid"
    expect(Object.keys(body).sort()).toEqual(
      ["canvas_filters_json", "layout_json"].sort(),
    );
    expect(body.layout_json).toEqual(layout);
    expect(body.canvas_filters_json).toEqual(filters);
  });

  it("sends Content-Type: application/json", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    await saveDashboard({ version: 1, widgets: [] }, {});

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers as HeadersInit);
    expect(headers.get("content-type")).toContain("application/json");
  });

  it("returns the updated DashboardLayoutResponse", async () => {
    const updated: DashboardLayoutResponse = {
      ...MOCK_LAYOUT,
      updated_at: "2026-06-24T11:00:00Z",
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(updated));

    const result = await saveDashboard({ version: 1, widgets: [] }, {});

    expect(result).toEqual(updated);
    expect(result.updated_at).toBe("2026-06-24T11:00:00Z");
  });

  it("includes the Bearer token in the Authorization header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(MOCK_LAYOUT));

    await saveDashboard({ version: 1, widgets: [] }, {});

    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers as HeadersInit);
    expect(headers.get("authorization")).toBe("Bearer test-bearer-token");
  });
});
