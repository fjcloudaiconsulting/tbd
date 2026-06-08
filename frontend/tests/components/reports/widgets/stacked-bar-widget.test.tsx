import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import StackedBarWidget from "@/components/reports/widgets/StackedBarWidget";
import type { StackedBarWidget as StackedBarWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

function makeWidget(overrides: Partial<StackedBarWidgetType["config"]> = {}): StackedBarWidgetType {
  return {
    id: `w_sb_${Math.random().toString(36).slice(2, 10)}`,
    type: "stacked_bar",
    title: "Income vs expense by month",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      format: "currency",
      stacked: true,
      ...overrides,
    },
  };
}

describe("StackedBarWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the chart wrapper and title when rows arrive", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ month: "2026-01", value: 100 }],
      meta: { row_count: 1, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<StackedBarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.queryByTestId("stacked-bar-widget-loading")).toBeNull(),
    );
    expect(screen.getByTestId("stacked-bar-widget")).toBeInTheDocument();
    expect(screen.getByText("Income vs expense by month")).toBeInTheDocument();
  });

  it("renders the empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderWithSWR(<StackedBarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(
        screen.getByTestId("stacked-bar-widget-empty"),
      ).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderWithSWR(<StackedBarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(
        screen.getByTestId("stacked-bar-widget-error"),
      ).toBeInTheDocument(),
    );
  });

  it("fires one query per series when multiple measures are configured", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ month: "2026-01", value: 100 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(
      <StackedBarWidget
        widget={makeWidget({
          measures: [
            { measure: { agg: "sum", field: "amount" } },
            { measure: { agg: "count", field: "id" } },
            { measure: { agg: "avg", field: "amount" } },
          ],
        })}
      />,
    );

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(3));
  });
});
