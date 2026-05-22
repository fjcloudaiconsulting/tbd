import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import LineWidget from "@/components/reports/widgets/LineWidget";
import type { LineWidget as LineWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

function makeWidget(overrides: Partial<LineWidgetType["config"]> = {}): LineWidgetType {
  return {
    id: `w_line_${Math.random().toString(36).slice(2, 10)}`,
    type: "line",
    title: "Income vs expense",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      format: "currency",
      ...overrides,
    },
  };
}

describe("LineWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the chart wrapper and title when rows arrive", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", value: 100 },
        { month: "2026-02", value: 180 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 5 },
    });

    renderIsolated(<LineWidget widget={makeWidget()} />);

    await waitFor(() => {
      expect(screen.queryByTestId("line-widget-empty")).toBeNull();
      expect(screen.queryByTestId("line-widget-loading")).toBeNull();
    });
    expect(screen.getByTestId("line-widget")).toBeInTheDocument();
    expect(screen.getByText("Income vs expense")).toBeInTheDocument();
  });

  it("renders the empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 1 },
    });

    renderIsolated(<LineWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("line-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("boom"));

    renderIsolated(<LineWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("line-widget-error")).toBeInTheDocument(),
    );
  });

  it("fires one query per series when multiple measures are configured", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ month: "2026-01", value: 100 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderIsolated(
      <LineWidget
        widget={makeWidget({
          measures: [
            { measure: { agg: "sum", field: "amount" }, label: "Total" },
            { measure: { agg: "count", field: "id" }, label: "Count" },
          ],
        })}
      />,
    );

    await waitFor(() => {
      // The hook issues both queries in parallel; assert the count.
      expect(runQueryMock).toHaveBeenCalledTimes(2);
    });
  });
});
