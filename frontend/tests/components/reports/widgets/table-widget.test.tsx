import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import TableWidget from "@/components/reports/widgets/TableWidget";
import type { TableWidget as TableWidgetType } from "@/lib/reports/types";
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

function makeWidget(overrides: Partial<TableWidgetType["config"]> = {}): TableWidgetType {
  return {
    id: `w_table_${Math.random().toString(36).slice(2, 10)}`,
    type: "table",
    title: "Top categories",
    grid: { x: 0, y: 0, w: 12, h: 6 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 100,
      format: "currency",
      ...overrides,
    },
  };
}

describe("TableWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders rows with dimension + measure columns when data arrives", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 1 },
    });

    renderIsolated(<TableWidget widget={makeWidget()} />);

    expect(await screen.findByText("Food")).toBeInTheDocument();
    expect(screen.getByText("Transport")).toBeInTheDocument();
  });

  it("renders an empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderIsolated(<TableWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("table-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderIsolated(<TableWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("table-widget-error")).toBeInTheDocument(),
    );
  });

  it("sorts ascending by dimension when the header is clicked", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Books", value: 80 },
        { category: "Coffee", value: 30 },
      ],
      meta: { row_count: 3, truncated: false, query_ms: 1 },
    });

    renderIsolated(<TableWidget widget={makeWidget()} />);

    await screen.findByText("Food");
    // First click on a fresh header => sortKey=category, dir=desc
    // (Food, Coffee, Books). Second click flips to ascending
    // (Books, Coffee, Food).
    fireEvent.click(screen.getByTestId("table-widget-sort-category"));
    fireEvent.click(screen.getByTestId("table-widget-sort-category"));
    await waitFor(() => {
      const widget = screen.getByTestId("table-widget");
      const cells = Array.from(widget.querySelectorAll("td"))
        .map((td) => td.textContent ?? "")
        .filter((t) => /^(Books|Coffee|Food)$/.test(t));
      // In ascending order the category column should read
      // Books, Coffee, Food top to bottom.
      expect(cells).toEqual(["Books", "Coffee", "Food"]);
    });
  });

  it("renders pagination when rows exceed PAGE_SIZE (50)", async () => {
    const rows = Array.from({ length: 75 }, (_, i) => ({
      category: `Cat ${i}`,
      value: i,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 75, truncated: false, query_ms: 1 },
    });

    renderIsolated(<TableWidget widget={makeWidget()} />);

    await screen.findByText("Cat 0");
    expect(
      screen.getByTestId("table-widget-pagination"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("table-widget-next-page")).toBeInTheDocument();
  });

  it("renders a Total row that sums all rows of a sum measure", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
        { category: "Books", value: 20 },
      ],
      meta: { row_count: 3, truncated: false, query_ms: 1 },
    });

    renderIsolated(<TableWidget widget={makeWidget()} />);

    const totalRow = await screen.findByTestId("table-widget-total-row");
    // Dimension cell reads "Total".
    expect(totalRow).toHaveTextContent("Total");
    // 200 + 80 + 20 = 300, formatted as currency.
    expect(totalRow).toHaveTextContent("$300.00");
  });

  it("sums ALL rows in the total even across multiple pages", async () => {
    // 75 rows, value = 1 each => grand total 75, even though only 50
    // are visible on the first page.
    const rows = Array.from({ length: 75 }, (_, i) => ({
      category: `Cat ${i}`,
      value: 1,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 75, truncated: false, query_ms: 1 },
    });

    renderIsolated(<TableWidget widget={makeWidget({ format: "number" })} />);

    const totalRow = await screen.findByTestId("table-widget-total-row");
    expect(totalRow).toHaveTextContent("Total");
    expect(totalRow).toHaveTextContent("75");
  });

  it("shows a placeholder (no fabricated number) for an avg measure column", async () => {
    runQueryMock.mockResolvedValue({
      rows: [
        { category: "Food", value: 50 },
        { category: "Transport", value: 30 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 1 },
    });

    renderIsolated(
      <TableWidget
        widget={makeWidget({
          format: "number",
          measures: [
            { measure: { agg: "sum", field: "amount" }, label: "Total" },
            { measure: { agg: "avg", field: "amount" }, label: "Avg" },
          ],
        })}
      />,
    );

    const totalRow = await screen.findByTestId("table-widget-total-row");
    const cells = Array.from(totalRow.querySelectorAll("td")).map(
      (td) => td.textContent ?? "",
    );
    // cells: [dimension "Total", sum column "80", avg column placeholder]
    expect(cells[0]).toBe("Total");
    expect(cells[1]).toBe("80");
    // avg column must NOT be a number; it's the placeholder.
    expect(cells[2]).toBe("—");
  });

  it("totals each measure column independently (multi-measure)", async () => {
    // Two series queries: sum-of-amount and count-of-id.
    runQueryMock
      .mockResolvedValueOnce({
        rows: [
          { category: "Food", value: 200 },
          { category: "Transport", value: 100 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 1 },
      })
      .mockResolvedValueOnce({
        rows: [
          { category: "Food", value: 4 },
          { category: "Transport", value: 6 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 1 },
      });

    renderIsolated(
      <TableWidget
        widget={makeWidget({
          format: "number",
          measures: [
            { measure: { agg: "sum", field: "amount" }, label: "Amount" },
            { measure: { agg: "count", field: "id" }, label: "Count" },
          ],
        })}
      />,
    );

    const totalRow = await screen.findByTestId("table-widget-total-row");
    const cells = Array.from(totalRow.querySelectorAll("td")).map(
      (td) => td.textContent ?? "",
    );
    expect(cells[0]).toBe("Total");
    expect(cells[1]).toBe("300"); // 200 + 100
    expect(cells[2]).toBe("10"); // 4 + 6 (count is additive)
  });

  it("fires one query per column when multiple measures are configured", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ category: "Food", value: 50 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderIsolated(
      <TableWidget
        widget={makeWidget({
          measures: [
            { measure: { agg: "sum", field: "amount" }, label: "Total" },
            { measure: { agg: "count", field: "id" }, label: "Count" },
            { measure: { agg: "avg", field: "amount" }, label: "Avg" },
          ],
        })}
      />,
    );

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(3));
  });
});
