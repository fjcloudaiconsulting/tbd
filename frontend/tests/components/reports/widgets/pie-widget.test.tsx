import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import PieWidget from "@/components/reports/widgets/PieWidget";
import type { PieWidget as PieWidgetType } from "@/lib/reports/types";
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

function makeWidget(overrides: Partial<PieWidgetType["config"]> = {}): PieWidgetType {
  return {
    id: `w_pie_${Math.random().toString(36).slice(2, 10)}`,
    type: "pie",
    title: "Spend share",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
      format: "currency",
      top_n: 8,
      ...overrides,
    },
  };
}

describe("PieWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the pie wrapper and title when rows arrive", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 100 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 3 },
    });

    renderIsolated(<PieWidget widget={makeWidget()} />);

    await waitFor(() => {
      expect(screen.queryByTestId("pie-widget-empty")).toBeNull();
      expect(screen.queryByTestId("pie-widget-loading")).toBeNull();
    });
    expect(screen.getByTestId("pie-widget")).toBeInTheDocument();
    expect(screen.getByText("Spend share")).toBeInTheDocument();
  });

  it("renders the empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderIsolated(<PieWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("pie-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderIsolated(<PieWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("pie-widget-error")).toBeInTheDocument(),
    );
  });
});
