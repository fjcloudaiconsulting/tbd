/**
 * TBD-382 DoD 3 — adding or removing a series on a Line widget visibly
 * changes the chart AND the export.
 *
 * Fences F18, F26.
 *
 * ⚠ F18 drives the ADD through the real editor control, not by hand-writing a
 * second measure. That is the whole point: the shipped defect was in the SEED
 * (`{agg:"sum", field: fields[0]}`), so a fence that hand-builds a distinct
 * second measure is vacuous — the renderer was never broken.
 *
 * ⚠ Asserting only that the two `runQuery` payloads DIFFER is the next-order
 * trap: on `networth`, `build_rows` ignores `measure.agg`/`measure.field`
 * entirely, so differing payloads return byte-identical rows. This fence
 * therefore reads the RENDERED series and the CSV VALUES.
 *
 * ⚠ Reordering is NOT fenced: `MeasuresEditor` offers add and remove only —
 * there is no reorder control anywhere in the app, and adding one is a new
 * interaction flow (a design change). Filed as a follow-up (R14).
 */
import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import LineWidget from "@/components/reports/widgets/LineWidget";
import { runQuery } from "@/lib/reports/api";
import { downloadCsv } from "@/lib/reports/csv";
import { mockReportSources } from "../../../utils/mock-report-sources";
import type {
  LineWidget as LineWidgetType,
  ReportsQuery,
  SeriesConfig,
  Widget,
} from "@/lib/reports/types";

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

vi.mock("@/components/reports/widgets/LineWidgetChart", () => ({
  default: (props: {
    rows: Array<Record<string, number | string>>;
    seriesKeys: string[];
    labels: string[];
  }) => (
    <div
      data-testid="line-chart-stub"
      data-rows={JSON.stringify(props.rows)}
      data-series-keys={JSON.stringify(props.seriesKeys)}
      data-labels={JSON.stringify(props.labels)}
    />
  ),
}));

/** Distinct value per (agg, field) pair, so identical payloads are visible. */
const VALUE_BY_PAIR: Record<string, number> = {
  "sum:amount": 100,
  "avg:amount": 25,
  "count:id": 3,
};

function makeLine(measures: SeriesConfig[]): LineWidgetType {
  return {
    id: "w_line",
    type: "line",
    title: "Spend over time",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: { dataset: "transactions", measures, dimensions: ["month"] },
  };
}

describe("DoD 3 — add / remove a series changes the rendered chart and the CSV", () => {
  const runQueryMock = vi.mocked(runQuery);
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => {
    runQueryMock.mockReset();
    downloadMock.mockReset();
    runQueryMock.mockImplementation(async (q: ReportsQuery) => {
      const key = `${q.measure.agg}:${q.measure.field}`;
      return {
        rows: [{ month: "2026-01", value: VALUE_BY_PAIR[key] ?? 0 }],
        meta: { row_count: 1, truncated: false, query_ms: 1 },
      };
    });
  });

  async function csvOf(widget: LineWidgetType): Promise<string[]> {
    renderWithSWR(<LineWidget widget={widget} />);
    await screen.findByTestId("line-chart-stub");
    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);
    const [, csv] = downloadMock.mock.calls.at(-1)!;
    return (csv as string).split("\r\n");
  }

  // ── F18 ───────────────────────────────────────────────────────────────
  it("F18: adding a series through the editor draws a SECOND, DIFFERENT line and a second CSV column", async () => {
    const updates: Widget[] = [];
    const first = renderWithSWR(
      <DataTab
        widget={makeLine([{ measure: { agg: "sum", field: "amount" } }])}
        onUpdate={(w) => updates.push(w)}
      />,
    );

    const addBtn = await screen.findByTestId("measure-add");
    await waitFor(() => expect(addBtn).toBeEnabled());
    fireEvent.click(addBtn);
    first.unmount();

    const widened = updates.at(-1) as LineWidgetType;
    expect(widened.config.measures).toHaveLength(2);

    const lines = await csvOf(widened);
    const stub = screen.getByTestId("line-chart-stub");
    const seriesKeys: string[] = JSON.parse(
      stub.getAttribute("data-series-keys") ?? "[]",
    );
    const rows: Array<Record<string, number | string>> = JSON.parse(
      stub.getAttribute("data-rows") ?? "[]",
    );

    // Two rendered series…
    expect(seriesKeys).toEqual(["s0", "s1"]);
    // …carrying DIFFERENT values. A duplicate seed draws pixel-identical.
    expect(rows[0][seriesKeys[0]]).not.toBe(rows[0][seriesKeys[1]]);

    // …and the CSV shows two distinctly-headed, distinctly-valued columns.
    const header = lines[0].split(",");
    expect(header).toHaveLength(3);
    expect(header[1]).not.toBe(header[2]);
    const values = lines[1].split(",").slice(1);
    expect(values[0]).not.toBe(values[1]);
    expect(values).toEqual(["100", "25"]);
  });

  // ── F26 ───────────────────────────────────────────────────────────────
  it("F26: removing a series through the editor removes the rendered line and its CSV column", async () => {
    const updates: Widget[] = [];
    const editor = renderWithSWR(
      <DataTab
        widget={makeLine([
          { measure: { agg: "sum", field: "amount" } },
          { measure: { agg: "avg", field: "amount" } },
        ])}
        onUpdate={(w) => updates.push(w)}
      />,
    );

    fireEvent.click(await screen.findByTestId("measure-remove-1"));
    editor.unmount();

    const narrowed = updates.at(-1) as LineWidgetType;
    expect(narrowed.config.measures).toHaveLength(1);

    const lines = await csvOf(narrowed);
    const stub = screen.getByTestId("line-chart-stub");
    expect(JSON.parse(stub.getAttribute("data-series-keys") ?? "[]")).toEqual([
      "s0",
    ]);

    expect(lines[0].split(",")).toHaveLength(2);
    expect(lines[1].split(",").slice(1)).toEqual(["100"]);
    // The removed series' value is gone from the export, not merely hidden.
    expect(lines[1]).not.toContain("25");
  });
});
