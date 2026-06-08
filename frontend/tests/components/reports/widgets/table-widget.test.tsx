import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import TableWidget from "@/components/reports/widgets/TableWidget";
import type { TableWidget as TableWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { downloadCsv } from "@/lib/reports/csv";

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

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
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => {
    runQueryMock.mockReset();
    downloadMock.mockReset();
  });

  it("renders rows with dimension + measure columns when data arrives", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    expect(await screen.findByText("Food")).toBeInTheDocument();
    expect(screen.getByText("Transport")).toBeInTheDocument();
  });

  it("renders an empty state with no rows", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [],
      meta: { row_count: 0, truncated: false, query_ms: 0 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("table-widget-empty")).toBeInTheDocument(),
    );
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("nope"));

    renderWithSWR(<TableWidget widget={makeWidget()} />);

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

    renderWithSWR(<TableWidget widget={makeWidget()} />);

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

  it("renders pagination when rows exceed the page size", async () => {
    // 75 rows with the default page size of 25 => 3 pages, so pagination is shown.
    // The shared Pagination component uses accessible button names ("Next page" /
    // "Previous page") rather than data-testid attributes, so we query by role.
    const rows = Array.from({ length: 75 }, (_, i) => ({
      category: `Cat ${i}`,
      value: i,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 75, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    await screen.findByText("Cat 0");
    expect(
      screen.getByTestId("table-widget-pagination"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Next page" }),
    ).toBeInTheDocument();
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

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    const totalRow = await screen.findByTestId("table-widget-total-row");
    // Dimension cell reads "Total".
    expect(totalRow).toHaveTextContent("Total");
    // 200 + 80 + 20 = 300, formatted as currency.
    expect(totalRow).toHaveTextContent("$300.00");
  });

  it("sums ALL rows in the total even across multiple pages", async () => {
    // 75 rows, value = 1 each => grand total 75, even though only 25
    // (the default page size) are visible on the first page.
    const rows = Array.from({ length: 75 }, (_, i) => ({
      category: `Cat ${i}`,
      value: 1,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 75, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget({ format: "number" })} />);

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

    renderWithSWR(
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

    renderWithSWR(
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

    renderWithSWR(
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

  it("exports CSV with dimension + measure headers, every row, and a Total row", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { category: "Food", value: 200 },
        { category: "Transport", value: 80 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);

    expect(downloadMock).toHaveBeenCalledTimes(1);
    const [filename, csv] = downloadMock.mock.calls[0];
    expect(filename).toBe("top-categories.csv");
    // Header uses the human dimension label "Category" + the measure
    // field "amount"; the Total row sums the additive column (280).
    expect(csv).toBe(
      "Category,amount\r\nFood,200\r\nTransport,80\r\nTotal,280",
    );
  });

  it("exports one column per measure for a multi-measure table", async () => {
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

    renderWithSWR(
      <TableWidget
        widget={makeWidget({
          measures: [
            { measure: { agg: "sum", field: "amount" }, label: "Amount" },
            { measure: { agg: "count", field: "id" }, label: "Count" },
          ],
        })}
      />,
    );

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);

    const [, csv] = downloadMock.mock.calls[0];
    expect(csv).toBe(
      "Category,Amount,Count\r\nFood,200,4\r\nTransport,100,6\r\nTotal,300,10",
    );
  });

  it("hides the Export CSV button in edit mode", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ category: "Food", value: 200 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} editMode />);

    await screen.findByText("Food");
    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  it("per-page selector limits visible rows and resets to the first page", async () => {
    // 30 rows; default page size 25 shows the first 25 on page 1.
    // Switching to 10 per page should show only 10 rows and reset to page 1.
    const rows = Array.from({ length: 30 }, (_, i) => ({
      category: `Cat ${i}`,
      value: i,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 30, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    // Wait for data and confirm 25 rows visible on the first page.
    await screen.findByText("Cat 0");
    // Advance to page 2 before changing page size so we can confirm reset.
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(screen.queryByText("Cat 0")).not.toBeInTheDocument(),
    );

    // Change per-page to 10.
    fireEvent.change(
      screen.getByRole("combobox", { name: "Per page" }),
      { target: { value: "10" } },
    );

    // Should snap back to page 1 — Cat 0 visible again.
    await waitFor(() =>
      expect(screen.getByText("Cat 0")).toBeInTheDocument(),
    );
    // Cat 10 should NOT be visible (only rows 0–9 on first page of 10).
    expect(screen.queryByText("Cat 10")).not.toBeInTheDocument();
  });

  it("Next and Previous buttons navigate in-memory rows", async () => {
    // 30 rows with default page size 25: page 1 shows Cat 0..24, page 2 shows Cat 25..29.
    const rows = Array.from({ length: 30 }, (_, i) => ({
      category: `Cat ${i}`,
      value: i,
    }));
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: 30, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<TableWidget widget={makeWidget()} />);

    await screen.findByText("Cat 0");
    // Previous should be disabled on page 1.
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

    // Navigate to page 2.
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() =>
      expect(screen.getByText("Cat 25")).toBeInTheDocument(),
    );
    expect(screen.queryByText("Cat 0")).not.toBeInTheDocument();

    // Next should now be disabled (last page).
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    // Navigate back.
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() =>
      expect(screen.getByText("Cat 0")).toBeInTheDocument(),
    );
    expect(screen.queryByText("Cat 25")).not.toBeInTheDocument();
  });
});
