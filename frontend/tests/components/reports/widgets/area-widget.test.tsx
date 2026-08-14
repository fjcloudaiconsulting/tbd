import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import AreaWidget from "@/components/reports/widgets/AreaWidget";
import type { AreaWidget as AreaWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  // TBD-381: format now derives at render from the source catalog, which
  // fetches via apiFetch. Without this the catalog is empty, format is
  // undefined, and the widget holds its loading skeleton forever.
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

function makeWidget(): AreaWidgetType {
  return {
    id: `w_area_${Math.random().toString(36).slice(2, 10)}`,
    type: "area",
    title: "Spend trend",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      stacked: false,
    },
  };
}

describe("AreaWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the chart wrapper and title when rows arrive", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ month: "2026-01", value: 100 }],
      meta: { row_count: 1, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<AreaWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.queryByTestId("area-widget-loading")).toBeNull(),
    );
    expect(screen.getByTestId("area-widget")).toBeInTheDocument();
    expect(screen.getByText("Spend trend")).toBeInTheDocument();
  });

  it("renders the empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderWithSWR(<AreaWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("area-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderWithSWR(<AreaWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("area-widget-error")).toBeInTheDocument(),
    );
  });
});
