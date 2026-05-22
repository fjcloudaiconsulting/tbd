import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import BarWidget from "@/components/reports/widgets/BarWidget";
import type { BarWidget as BarWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

// Recharts renders SVG via ResponsiveContainer; jsdom doesn't compute
// layout, so the ResponsiveContainer collapses to 0×0 and skips the
// chart body. We assert on the data-binding path (chart container
// present, no error / empty state) rather than the rendered bars.
function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

function makeWidget(): BarWidgetType {
  return {
    id: `w_bar_${Math.random().toString(36).slice(2, 10)}`,
    type: "bar",
    title: "Spend by category",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 10,
      format: "currency",
    },
  };
}

describe("BarWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the chart surface and the title when rows are present", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 4 },
    });

    renderIsolated(<BarWidget widget={makeWidget()} />);

    await waitFor(() => {
      // Empty / loading states must NOT be shown when rows arrive.
      expect(screen.queryByTestId("bar-widget-empty")).toBeNull();
      expect(screen.queryByTestId("bar-widget-loading")).toBeNull();
    });
    // The widget container always renders; the title comes from
    // ``widget.title`` so the chart wrapper is locatable.
    expect(screen.getByTestId("bar-widget")).toBeInTheDocument();
    expect(screen.getByText("Spend by category")).toBeInTheDocument();
  });

  it("renders the empty state when rows is an empty array", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 2 },
    });

    renderIsolated(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("bar-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderIsolated(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("bar-widget-error")).toBeInTheDocument(),
    );
  });
});
