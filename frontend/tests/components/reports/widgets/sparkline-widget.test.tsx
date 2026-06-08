import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import type { SparklineWidget as SparklineWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

function makeWidget(): SparklineWidgetType {
  return {
    id: `w_spark_${Math.random().toString(36).slice(2, 10)}`,
    type: "sparkline",
    title: "Cash flow",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      format: "number",
    },
  };
}

describe("SparklineWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the last value as the headline number", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", value: 100 },
        { month: "2026-02", value: 220 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<SparklineWidget widget={makeWidget()} />);

    const value = await screen.findByTestId("sparkline-widget-value");
    // Last row was 220 -> headline shows it.
    expect(value.textContent).toContain("220");
  });

  it("renders the empty fallback with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderWithSWR(<SparklineWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("sparkline-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderWithSWR(<SparklineWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("sparkline-widget-error")).toBeInTheDocument(),
    );
  });
});
