import { renderWithSWR, screen, waitFor, fireEvent } from "../../../utils/render-with-swr";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * @nivo/sankey uses ResizeObserver + DOM layout that don't work in jsdom.
 * Mock ResponsiveSankey so it renders its props as data attributes — lets
 * us assert on the converted data/colors that SankeyWidgetChart forwards
 * without needing a real DOM layout pass.
 */
vi.mock("@nivo/sankey", () => ({
  ResponsiveSankey: ({
    data,
    colors,
  }: {
    data: { nodes: { id: string }[]; links: { source: string; target: string; value: number }[] };
    colors: readonly string[];
  }) => (
    <div
      data-testid="nivo-sankey"
      data-nodes={JSON.stringify(data.nodes)}
      data-links={JSON.stringify(data.links)}
      data-colors={JSON.stringify(colors)}
    />
  ),
}));

vi.mock("@/lib/reports/useSankeyQuery", () => ({
  useSankeyQuery: vi.fn(),
}));

// Keep the real CSV serializer (so we assert the real output string) but stub
// the DOM download so jsdom doesn't try to click an <a download>.
vi.mock("@/lib/reports/csv", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

import SankeyWidget from "@/components/reports/widgets/SankeyWidget";
import type { SankeyWidget as SankeyWidgetType } from "@/lib/reports/types";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
import { downloadCsv } from "@/lib/reports/csv";
import { CHART_SERIES } from "@/lib/chart-colors";

function makeWidget(overrides: Partial<SankeyWidgetType["config"]> = {}): SankeyWidgetType {
  return {
    id: "w_sankey_test",
    type: "sankey",
    title: "Cash flow",
    grid: { x: 0, y: 0, w: 8, h: 5 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      spending_granularity: "category",
      ...overrides,
    },
  };
}

describe("SankeyWidget", () => {
  const mockUseSankeyQuery = vi.mocked(useSankeyQuery);

  beforeEach(() => {
    mockUseSankeyQuery.mockReset();
  });

  it("renders the chart when links are present", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [
          { source: "__hub_income__", target: "Food", value: 200 },
          { source: "__hub_income__", target: "Transport", value: 80 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 4 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("nivo-sankey")).toBeInTheDocument(),
    );

    // Should not show empty/error states
    expect(screen.queryByTestId("sankey-widget-empty")).toBeNull();
    expect(screen.queryByTestId("sankey-widget-error")).toBeNull();
  });

  it("offers a CSV export button (enabled) when links are present", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [
          { source: "__hub_income__", target: "Food", value: 200 },
          { source: "__hub_income__", target: "Transport", value: 80 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 4 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    const csv = await screen.findByTestId("widget-csv-export");
    expect(csv).toBeInTheDocument();
    expect(csv).not.toBeDisabled();
  });

  it("exports friendly hub labels in the CSV, not raw sentinel ids", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [{ source: "__hub_income__", target: "Food", value: 200 }],
        meta: { row_count: 1, truncated: false, query_ms: 2 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);
    fireEvent.click(await screen.findByTestId("widget-csv-export"));

    expect(downloadCsv).toHaveBeenCalledTimes(1);
    const csvString = vi.mocked(downloadCsv).mock.calls[0][1];
    expect(csvString).toContain("Income"); // __hub_income__ mapped to label
    expect(csvString).not.toContain("__hub_income__");
    expect(csvString).toContain("Food"); // real category id passes through
  });

  it("disables the CSV export button in the empty (no-links) state", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: { links: [], meta: { row_count: 0, truncated: false, query_ms: 1 } },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);
    expect(await screen.findByTestId("widget-csv-export")).toBeDisabled();
  });

  it("hides the CSV export button in edit mode", () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [{ source: "__hub_income__", target: "Food", value: 200 }],
        meta: { row_count: 1, truncated: false, query_ms: 2 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} editMode />);

    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  it("passes converted nodes/links and CHART_SERIES colors to ResponsiveSankey", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [
          { source: "__hub_income__", target: "Food", value: 200 },
          { source: "__hub_income__", target: "Transport", value: 80 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 4 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    const chart = await screen.findByTestId("nivo-sankey");

    // Nodes: unique ids from links — sentinel id is preserved as the node id
    // (label mapping is done via the label accessor, not by rewriting the id).
    const nodes = JSON.parse(chart.getAttribute("data-nodes") ?? "[]") as { id: string }[];
    const nodeIds = nodes.map((n) => n.id).sort();
    expect(nodeIds).toEqual(["Food", "Transport", "__hub_income__"]);

    // Links: pass-through from SankeyLink[] with sentinel ids on the wire
    const links = JSON.parse(chart.getAttribute("data-links") ?? "[]") as {
      source: string;
      target: string;
      value: number;
    }[];
    expect(links).toHaveLength(2);
    expect(links[0]).toEqual({ source: "__hub_income__", target: "Food", value: 200 });
    expect(links[1]).toEqual({ source: "__hub_income__", target: "Transport", value: 80 });

    // Colors: must be CHART_SERIES
    const colors = JSON.parse(chart.getAttribute("data-colors") ?? "[]") as string[];
    expect(colors).toEqual(Array.from(CHART_SERIES));
  });

  it("renders the empty state when links is an empty array", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [],
        meta: { row_count: 0, truncated: false, query_ms: 1 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("sankey-widget-empty")).toBeInTheDocument(),
    );

    expect(screen.getByTestId("sankey-widget-empty")).toHaveTextContent(
      "No income in this period to chart cash flow",
    );
    expect(screen.queryByTestId("nivo-sankey")).toBeNull();
  });

  it(
    // SWR has resolved (isLoading=false) but returned undefined data — this
    // happens when the SWR cache entry is stale-while-revalidating and the
    // previous value was undefined (e.g. first mount before any fetch completes
    // in a non-loading SWR state). Treated as empty, not as loading.
    "renders the empty state when SWR resolves with undefined data (stale/uninitialised cache)",
    async () => {
      mockUseSankeyQuery.mockReturnValue({
        data: undefined,
        error: undefined,
        isLoading: false,
        query: { filters: [], spending_granularity: "category" },
      });

      renderWithSWR(<SankeyWidget widget={makeWidget()} />);

      await waitFor(() =>
        expect(screen.getByTestId("sankey-widget-empty")).toBeInTheDocument(),
      );
    },
  );

  it("renders a loading skeleton while loading", () => {
    mockUseSankeyQuery.mockReturnValue({
      data: undefined,
      error: undefined,
      isLoading: true,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    expect(screen.getByTestId("sankey-widget-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("sankey-widget-empty")).toBeNull();
    expect(screen.queryByTestId("nivo-sankey")).toBeNull();
  });

  it("renders an inline error when the query fails", () => {
    mockUseSankeyQuery.mockReturnValue({
      data: undefined,
      error: new Error("network error"),
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    expect(screen.getByTestId("sankey-widget-error")).toBeInTheDocument();
    expect(screen.queryByTestId("sankey-widget-empty")).toBeNull();
    expect(screen.queryByTestId("nivo-sankey")).toBeNull();
  });

  it("renders the widget title", () => {
    mockUseSankeyQuery.mockReturnValue({
      data: { links: [], meta: { row_count: 0, truncated: false, query_ms: 1 } },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    expect(screen.getByText("Cash flow")).toBeInTheDocument();
  });

  it("passes canvasFilters directly to useSankeyQuery (no fresh-object wrap)", () => {
    const canvasFilters = { date_range: { start: "2026-01-01", end: "2026-01-31" } };
    mockUseSankeyQuery.mockReturnValue({
      data: undefined,
      error: undefined,
      isLoading: true,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} canvasFilters={canvasFilters} />);

    // The hook is called with the exact canvasFilters reference, not a
    // fresh {} fallback that would defeat useMemo in useSankeyQuery.
    expect(mockUseSankeyQuery).toHaveBeenCalledWith(
      expect.anything(),
      canvasFilters,
    );
  });
});
