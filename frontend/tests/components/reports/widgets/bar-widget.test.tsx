import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import BarWidget from "@/components/reports/widgets/BarWidget";
import type { BarWidget as BarWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { downloadCsv } from "@/lib/reports/csv";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

// Mock only the download side effect; keep the real toCsv / csvFilename
// so the test asserts the actual serialized CSV string.
vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

// Recharts renders SVG via ResponsiveContainer; jsdom doesn't compute
// layout, so the ResponsiveContainer collapses to 0×0 and skips the
// chart body. We assert on the data-binding path (chart container
// present, no error / empty state) and on the DOM legend the widget
// renders itself (outside the SVG) when a break-down dimension is set.
function makeWidget(
  overrides: Partial<BarWidgetType["config"]> = {},
): BarWidgetType {
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
      ...overrides,
    },
  };
}

describe("BarWidget", () => {
  const runQueryMock = vi.mocked(runQuery);
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => {
    runQueryMock.mockReset();
    downloadMock.mockReset();
  });

  it("renders the chart surface and the title when rows are present", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 4 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

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

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("bar-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("bar-widget-error")).toBeInTheDocument(),
    );
  });

  it("renders single-series bars with no per-account legend when no break-down dimension is set", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 4 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.queryByTestId("bar-widget-loading")).toBeNull(),
    );
    // No break-down → the per-series legend must be absent.
    expect(screen.queryByTestId("bar-widget-legend")).toBeNull();
  });

  it("queries grouped by both dimensions and renders a per-account legend when broken down by account", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", account: "Checking", value: 120 },
        { category: "Food", account: "Savings", value: 40 },
        { category: "Transport", account: "Checking", value: 60 },
        { category: "Transport", account: "Credit Card", value: 25 },
      ],
      meta: { row_count: 4, truncated: false, query_ms: 6 },
    });

    renderWithSWR(
      <BarWidget widget={makeWidget({ dimensions: ["category", "account"] })} />,
    );

    // One query, grouped by both dimensions.
    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(runQueryMock.mock.calls[0][0].dimensions).toEqual([
      "category",
      "account",
    ]);

    // Legend lists every distinct account, each with its own swatch color.
    const legend = await screen.findByTestId("bar-widget-legend");
    expect(legend).toBeInTheDocument();

    const items = screen.getAllByTestId("bar-widget-legend-item");
    expect(items).toHaveLength(3);
    expect(legend).toHaveTextContent("Checking");
    expect(legend).toHaveTextContent("Savings");
    expect(legend).toHaveTextContent("Credit Card");

    // Each legend swatch carries a distinct color.
    const swatches = screen
      .getAllByTestId("bar-widget-legend-swatch")
      .map((el) => el.getAttribute("data-color"));
    expect(new Set(swatches).size).toBe(swatches.length);
  });

  it("shows the Export CSV button in view mode and downloads the displayed single-series rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 4 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const exportBtn = await screen.findByTestId("widget-csv-export");
    expect(exportBtn).toBeInTheDocument();
    await waitFor(() => expect(exportBtn).not.toBeDisabled());

    fireEvent.click(exportBtn);

    expect(downloadMock).toHaveBeenCalledTimes(1);
    const [filename, csv] = downloadMock.mock.calls[0];
    expect(filename).toBe("spend-by-category.csv");
    expect(csv).toBe("Category,amount\r\nFood,200\r\nTransport,80");
  });

  it("exports one column per account when broken down by a secondary dimension", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", account: "Checking", value: 120 },
        { category: "Food", account: "Savings", value: 40 },
        { category: "Transport", account: "Checking", value: 60 },
      ],
      meta: { row_count: 3, truncated: false, query_ms: 6 },
    });

    renderWithSWR(
      <BarWidget widget={makeWidget({ dimensions: ["category", "account"] })} />,
    );

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);

    const [, csv] = downloadMock.mock.calls[0];
    // Primary dimension column + one column per distinct account; missing
    // (Transport, Savings) backfilled with 0.
    expect(csv).toBe(
      "Category,Checking,Savings\r\nFood,120,40\r\nTransport,60,0",
    );
  });

  it("hides the Export CSV button in edit mode", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ category: "Food", value: 200 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} editMode />);

    await waitFor(() =>
      expect(screen.queryByTestId("bar-widget-loading")).toBeNull(),
    );
    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });
});
