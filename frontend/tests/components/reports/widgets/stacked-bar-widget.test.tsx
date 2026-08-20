/**
 * TBD-382 — `stacked_bar` renders through BarWidget and stacks by
 * `config.dimensions[1]`, not by `config.measures`.
 *
 * Fences F1, F2, F3, F4, F8, F9, F25, F30, F31 plus the R10 plumbing guard.
 *
 * ⚠ Every fence here MOUNTS the widget. A unit test of
 * `pivotBySecondaryDimension` alone is vacuous: that function is already
 * correct on `main` and the shipped defect was that `StackedBarWidget` never
 * called it (it called `mergeSeriesRows`, which is last-write-wins on the
 * primary label).
 */
import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import BarWidget from "@/components/reports/widgets/BarWidget";
import type { StackedBarWidget as StackedBarWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";
import { downloadCsv } from "@/lib/reports/csv";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

/**
 * ⚠ F31's whole point: `config.stacked` only manifests inside a recharts
 * subtree that jsdom collapses to 0×0, so hardcoding `stacked = true` in the
 * widget leaves the entire suite green. Mocking the chart MODULE does
 * intercept the `next/dynamic` import, so the stub below is the only place
 * the boolean (and the pivoted series/colour arrays) is observable.
 */
vi.mock("@/components/reports/widgets/BarWidgetChart", () => ({
  default: (props: {
    rows: Array<Record<string, number | string>>;
    sliced: boolean;
    stacked: boolean;
    secondaryValues: string[];
    seriesKeys: string[];
    sliceColors: string[];
    valueName: string;
    format: string;
  }) => (
    <div
      data-testid="bar-chart-stub"
      data-stacked={String(props.stacked)}
      data-sliced={String(props.sliced)}
      data-format={props.format}
      data-value-name={props.valueName}
      data-series-keys={JSON.stringify(props.seriesKeys)}
      data-secondary-values={JSON.stringify(props.secondaryValues)}
      data-slice-colors={JSON.stringify(props.sliceColors)}
      data-rows={JSON.stringify(props.rows)}
    />
  ),
}));

function makeWidget(
  overrides: Partial<StackedBarWidgetType["config"]> = {},
): StackedBarWidgetType {
  return {
    id: `w_sb_${Math.random().toString(36).slice(2, 10)}`,
    type: "stacked_bar",
    title: "Category by month",
    grid: { x: 0, y: 0, w: 12, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month", "category"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      ...overrides,
    },
  };
}

async function chartStub() {
  return await screen.findByTestId("bar-chart-stub");
}

function rowsOf(el: HTMLElement): Array<Record<string, number | string>> {
  return JSON.parse(el.getAttribute("data-rows") ?? "[]");
}

function seriesOf(el: HTMLElement): string[] {
  return JSON.parse(el.getAttribute("data-series-keys") ?? "[]");
}

function secondariesOf(el: HTMLElement): string[] {
  return JSON.parse(el.getAttribute("data-secondary-values") ?? "[]");
}

describe("stacked_bar — breaks down by dimensions[1] (TBD-382)", () => {
  const runQueryMock = vi.mocked(runQuery);
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => {
    runQueryMock.mockReset();
    downloadMock.mockReset();
  });

  // ── F1 ────────────────────────────────────────────────────────────────
  it("F1: renders every (month, category) pair; the month total is NOT the smallest category", async () => {
    // 2 months × 3 categories, one pair deliberately missing, delivered in
    // the compiler's default `ORDER BY value DESC` over PAIRS — which is
    // exactly the ordering that made the old last-write-wins merge show
    // each month's SMALLEST category as the month's total.
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", category: "Rent", value: 900 },
        { month: "2026-02", category: "Rent", value: 900 },
        { month: "2026-01", category: "Groceries", value: 300 },
        { month: "2026-02", category: "Groceries", value: 250 },
        { month: "2026-01", category: "Coffee", value: 40 },
        // (2026-02, Coffee) intentionally absent -> backfilled to 0.
      ],
      meta: { row_count: 5, truncated: false, query_ms: 3 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const stub = await chartStub();
    const rows = rowsOf(stub);
    const keys = seriesOf(stub);
    const labels = secondariesOf(stub);

    // One row per PRIMARY (month), not per pair.
    expect(rows.map((r) => r.label)).toEqual(["2026-01", "2026-02"]);

    const total = (row: Record<string, number | string>) =>
      keys.reduce(
        (sum, k) => sum + (typeof row[k] === "number" ? (row[k] as number) : 0),
        0,
      );

    // The bar total is the SUM of the month's categories, not the last /
    // smallest one written.
    expect(total(rows[0])).toBe(1240);
    expect(total(rows[1])).toBe(1150);
    expect(total(rows[0])).not.toBe(40); // the smallest category
    expect(total(rows[1])).not.toBe(250);

    // The missing pair is backfilled with 0, not dropped.
    const coffeeKey = keys[labels.indexOf("Coffee")];
    expect(rows[1][coffeeKey]).toBe(0);

    // CSV totals agree with the rendered segments.
    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);
    const [, csv] = downloadMock.mock.calls[0];
    const lines = (csv as string).split("\r\n");
    const sumLine = (line: string) =>
      line
        .split(",")
        .slice(1)
        .reduce((s, v) => s + Number(v), 0);
    expect(sumLine(lines[1])).toBe(1240);
    expect(sumLine(lines[2])).toBe(1150);
  });

  // ── F2 ────────────────────────────────────────────────────────────────
  it("F2: one measure + two dimensions yields >=2 series keys and a legend of category names", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", category: "Rent", value: 900 },
        { month: "2026-01", category: "Groceries", value: 300 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const stub = await chartStub();
    expect(seriesOf(stub).length).toBeGreaterThanOrEqual(2);

    const legend = await screen.findByTestId("stacked-bar-widget-legend");
    expect(legend).toHaveTextContent("Rent");
    expect(legend).toHaveTextContent("Groceries");
  });

  // ── F3 ────────────────────────────────────────────────────────────────
  it("F3: fires exactly ONE query, grouped by both dimensions, at the MAX_LIMIT ceiling", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ month: "2026-01", category: "Rent", value: 900 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    const ast = runQueryMock.mock.calls[0][0];
    expect(ast.dimensions).toEqual(["month", "category"]);
    expect(ast.limit).toBe(500);
  });

  // ── F4 ────────────────────────────────────────────────────────────────
  it("F4: a legacy duplicate-measures config renders one series and is never rewritten at render", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ month: "2026-01", value: 900 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    const widget = makeWidget({
      dimensions: ["month"],
      measures: [
        { measure: { agg: "sum", field: "amount" } },
        { measure: { agg: "sum", field: "amount" } },
      ],
    });
    const snapshot = JSON.stringify(widget.config);

    renderWithSWR(<BarWidget widget={widget} />);

    const stub = await chartStub();
    // ONE series: the second (identical) measure is not a second axis.
    expect(seriesOf(stub)).toHaveLength(0); // unsliced: no per-secondary keys
    expect(stub.getAttribute("data-sliced")).toBe("false");
    expect(rowsOf(stub)).toEqual([{ label: "2026-01", value: 900 }]);
    // Exactly one query, not one per legacy measure.
    expect(runQueryMock).toHaveBeenCalledTimes(1);
    // Render mutates nothing that gets persisted.
    expect(JSON.stringify(widget.config)).toBe(snapshot);
  });

  // ── F8 ────────────────────────────────────────────────────────────────
  it("F8: the sliced CSV headers and rows agree with the rendered chart", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", category: "Groceries", value: 300 },
        { month: "2026-01", category: "Rent", value: 900 },
        { month: "2026-02", category: "Rent", value: 850 },
      ],
      meta: { row_count: 3, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const stub = await chartStub();
    const labels = secondariesOf(stub);
    const keys = seriesOf(stub);
    const rows = rowsOf(stub);

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);
    const [, csv] = downloadMock.mock.calls[0];
    const lines = (csv as string).split("\r\n");

    expect(lines[0]).toBe(["Month", ...labels].join(","));
    rows.forEach((row, i) => {
      const expected = [
        String(row.label),
        ...keys.map((k) => String(typeof row[k] === "number" ? row[k] : 0)),
      ].join(",");
      expect(lines[i + 1]).toBe(expected);
    });
  });

  // ── F9 (guard) ────────────────────────────────────────────────────────
  it("F9 guard: the format resolver still sees the real length-1 measure, not the pivoted s0..sN keys", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", category: "Rent", value: 900 },
        { month: "2026-01", category: "Groceries", value: 300 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const stub = await chartStub();
    // transactions sum(amount) publishes format "currency" in the catalog.
    expect(stub.getAttribute("data-format")).toBe("currency");
  });

  // ── F25 ───────────────────────────────────────────────────────────────
  it("F25: no secondary dimension renders one series, no legend, no crash", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", value: 900 },
        { month: "2026-02", value: 850 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 2 },
    });

    renderWithSWR(<BarWidget widget={makeWidget({ dimensions: ["month"] })} />);

    const stub = await chartStub();
    expect(stub.getAttribute("data-sliced")).toBe("false");
    expect(rowsOf(stub)).toEqual([
      { label: "2026-01", value: 900 },
      { label: "2026-02", value: 850 },
    ]);
    expect(screen.queryByTestId("stacked-bar-widget-legend")).toBeNull();
  });

  // ── F30 ───────────────────────────────────────────────────────────────
  it("F30: the config-shape adapter is read through the CSV header, not through runQuery", async () => {
    // ⚠ Asserting `runQuery` received {agg:"count", field:"id"} is VACUOUS:
    // `buildQueryAst` reads `config.measures[0].measure` itself, so a
    // hardcoded adapter inside the widget is masked entirely by that
    // downstream path. `count(id)` LABELS as "Row count" while the
    // hardcoded {sum, amount} fallback labels as "Amount", so the CSV
    // header is where the adapter is actually observable.
    runQueryMock.mockResolvedValueOnce({
      rows: [
        { month: "2026-01", value: 12 },
        { month: "2026-02", value: 9 },
      ],
      meta: { row_count: 2, truncated: false, query_ms: 2 },
    });

    renderWithSWR(
      <BarWidget
        widget={makeWidget({
          dimensions: ["month"],
          measures: [{ measure: { agg: "count", field: "id" } }],
        })}
      />,
    );

    const stub = await chartStub();
    expect(stub.getAttribute("data-value-name")).toBe("Row count");
    // And the same adapter drives the format resolver: count(id) is a
    // cardinality, never currency.
    expect(stub.getAttribute("data-format")).toBe("number");

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);
    const [, csv] = downloadMock.mock.calls[0];
    expect((csv as string).split("\r\n")[0]).toBe("Month,Row count");
  });

  // ── F31 ───────────────────────────────────────────────────────────────
  it.each([
    [undefined, "true"],
    [true, "true"],
    [false, "false"],
  ])(
    "F31: config.stacked=%s reaches the chart as stacked=%s",
    async (stacked, expected) => {
      runQueryMock.mockResolvedValueOnce({
        rows: [
          { month: "2026-01", category: "Rent", value: 900 },
          { month: "2026-01", category: "Groceries", value: 300 },
        ],
        meta: { row_count: 2, truncated: false, query_ms: 2 },
      });

      renderWithSWR(
        <BarWidget widget={makeWidget({ stacked: stacked as boolean | undefined })} />,
      );

      const stub = await chartStub();
      expect(stub.getAttribute("data-stacked")).toBe(expected);
    },
  );

  // ── R10 guard ─────────────────────────────────────────────────────────
  it("R10 guard: meta.truncated is plumbed to the widget even though nothing renders it yet", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ month: "2026-01", category: "Rent", value: 900 }],
      meta: { row_count: 500, truncated: true, query_ms: 9 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("stacked-bar-widget")).toHaveAttribute(
        "data-truncated",
        "true",
      ),
    );
  });
});
