import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";
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

import SankeyWidget from "@/components/reports/widgets/SankeyWidget";
import type { SankeyWidget as SankeyWidgetType } from "@/lib/reports/types";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
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
          { source: "Income", target: "Food", value: 200 },
          { source: "Income", target: "Transport", value: 80 },
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

  it("passes converted nodes/links and CHART_SERIES colors to ResponsiveSankey", async () => {
    mockUseSankeyQuery.mockReturnValue({
      data: {
        links: [
          { source: "Income", target: "Food", value: 200 },
          { source: "Income", target: "Transport", value: 80 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 4 },
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });

    renderWithSWR(<SankeyWidget widget={makeWidget()} />);

    const chart = await screen.findByTestId("nivo-sankey");

    // Nodes: unique ids from links (Income, Food, Transport)
    const nodes = JSON.parse(chart.getAttribute("data-nodes") ?? "[]") as { id: string }[];
    const nodeIds = nodes.map((n) => n.id).sort();
    expect(nodeIds).toEqual(["Food", "Income", "Transport"]);

    // Links: pass-through from SankeyLink[]
    const links = JSON.parse(chart.getAttribute("data-links") ?? "[]") as {
      source: string;
      target: string;
      value: number;
    }[];
    expect(links).toHaveLength(2);
    expect(links[0]).toEqual({ source: "Income", target: "Food", value: 200 });
    expect(links[1]).toEqual({ source: "Income", target: "Transport", value: 80 });

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

  it("renders the empty state when data is undefined (not yet loaded but no error)", async () => {
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
  });

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
});
