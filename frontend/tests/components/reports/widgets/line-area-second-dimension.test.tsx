/**
 * TBD-486 — `line` / `area` REFUSE a second dimension; they never draw one.
 *
 * The defect these fences pin: `mergeSeriesRows` keys on the PRIMARY
 * dimension and does `existing[key] = readNumber(row.value)` — an
 * ASSIGNMENT. With `dimensions: ["month", "category"]` the rows
 * `(2026-01, Groceries, 100)` and `(2026-01, Rent, 900)` both key to
 * `"2026-01"`, so the second overwrote the first and the chart plotted 900
 * as January's figure — the axis, the tooltip and the CSV all presenting one
 * arbitrary category's value as the whole month's. Same shape TBD-382 fixed
 * for `stacked_bar`.
 *
 * ⚠ Every fence here MOUNTS the real widget with real rows. A unit test of
 * `hasSecondDimension` would be vacuous: the predicate is trivially correct
 * and the shipped defect was that nothing consulted one.
 *
 * ⚠ The chart module is MOCKED, and that is what makes the wrong NUMBER
 * observable. jsdom collapses the recharts subtree to 0×0, so a fence that
 * only looked at the real chart could not tell "plotted 900" from "plotted
 * nothing" — and would stay green against the shipped defect. The stub
 * renders each point's value as text, so `textContent` is the assertion
 * surface.
 *
 * ⚠ The refusal PRESERVES the config. It is the same ruling as
 * `UNSUPPORTED_MEASURE_KEY` (`controlConstants.ts`): "Rewriting would change
 * the number a saved report renders without telling anyone." Repairing the
 * widget by dropping `dimensions[1]` would silently re-point the query at a
 * different question, which is why F3/F8 snapshot the config across the
 * render. Building the break-down is TBD-383, deliberately NOT this ticket.
 */
import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import type {
  AreaWidget as AreaWidgetType,
  LineWidget as LineWidgetType,
} from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { SECOND_DIMENSION_UNSUPPORTED_NOTICE } from "@/lib/reports/series";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

/** See the ⚠ in the module docstring: the stub is what makes 900 visible. */
function chartStub(testid: string) {
  function ChartStub(props: {
    rows: Array<Record<string, number | string>>;
    seriesKeys: string[];
  }) {
    return (
      <div data-testid={testid} data-rows={JSON.stringify(props.rows)}>
        {props.rows.map((r) => (
          <span key={String(r.label)} data-testid={`${testid}-point-${r.label}`}>
            {props.seriesKeys.map((k) => String(r[k])).join(",")}
          </span>
        ))}
      </div>
    );
  }
  return ChartStub;
}

vi.mock("@/components/reports/widgets/LineWidgetChart", () => ({
  default: chartStub("line-chart-stub"),
}));

vi.mock("@/components/reports/widgets/AreaWidgetChart", () => ({
  default: chartStub("area-chart-stub"),
}));

/**
 * Two categories inside ONE month, delivered in the compiler's default
 * `ORDER BY value DESC` — the ordering under which last-write-wins showed
 * each month's SMALLEST category as the month total. 900 is the last pair
 * returned for 2026-01; 40 is the last overall.
 */
const TWO_DIMENSION_ROWS = {
  rows: [
    { month: "2026-01", category: "Rent", value: 900 },
    { month: "2026-01", category: "Groceries", value: 100 },
    { month: "2026-02", category: "Rent", value: 800 },
    { month: "2026-02", category: "Coffee", value: 40 },
  ],
  meta: { row_count: 4, truncated: false, query_ms: 3 },
};

const ONE_DIMENSION_ROWS = {
  rows: [
    { month: "2026-01", value: 1000 },
    { month: "2026-02", value: 840 },
  ],
  meta: { row_count: 2, truncated: false, query_ms: 3 },
};

function makeLine(
  dimensions: LineWidgetType["config"]["dimensions"],
): LineWidgetType {
  return {
    id: "w_line_2d",
    type: "line",
    title: "Net by month",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions,
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

function makeArea(
  dimensions: AreaWidgetType["config"]["dimensions"],
): AreaWidgetType {
  return {
    id: "w_area_2d",
    type: "area",
    title: "Net by month",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions,
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

describe("line / area refuse a second dimension (TBD-486)", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  // ── F1 ────────────────────────────────────────────────────────────────
  it("F1: line does NOT plot the last returned pair's value as the month's value", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<LineWidget widget={makeLine(["month", "category"])} />);

    const widget = await screen.findByTestId("line-widget-unsupported");
    // Let the in-flight query settle so the assertion below runs against
    // the widget's FINAL render, not a pre-fetch frame that would pass
    // vacuously.
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    const card = screen.getByTestId("line-widget");
    // The number itself. 900 is Rent, the LAST pair returned for 2026-01;
    // last-write-wins drew it as the January point. 100 (Groceries, the
    // first pair) is equally wrong and equally forbidden.
    expect(card.textContent).not.toContain("900");
    expect(card.textContent).not.toContain("100");
    expect(card.textContent).not.toContain("40");
    // No point was drawn at all.
    expect(screen.queryByTestId("line-chart-stub")).toBeNull();
    expect(screen.queryByTestId("line-chart-stub-point-2026-01")).toBeNull();
    expect(widget).toBeInTheDocument();
  });

  // ── F2 ────────────────────────────────────────────────────────────────
  it("F2: line shows the unsupported state and NOT the chart", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<LineWidget widget={makeLine(["month", "category"])} />);

    const notice = await screen.findByTestId("line-widget-unsupported");
    expect(notice).toHaveTextContent(SECOND_DIMENSION_UNSUPPORTED_NOTICE);
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());
    expect(screen.queryByTestId("line-chart-stub")).toBeNull();
    expect(screen.queryByTestId("line-widget-chart-loading")).toBeNull();
    expect(screen.queryByTestId("line-widget-empty")).toBeNull();
  });

  // ── F3 ────────────────────────────────────────────────────────────────
  it("F3: line PRESERVES config.dimensions — the refusal never repairs it", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);
    const widget = makeLine(["month", "category"]);
    const before = JSON.stringify(widget.config);

    renderWithSWR(<LineWidget widget={widget} />);
    await screen.findByTestId("line-widget-unsupported");
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    expect(widget.config.dimensions).toEqual(["month", "category"]);
    // Whole-config snapshot: a "repair" anywhere in the object is caught,
    // not just one that touches dimensions.
    expect(JSON.stringify(widget.config)).toBe(before);
  });

  // ── F4 ────────────────────────────────────────────────────────────────
  it("F4: line withholds the CSV export — the merged rows are the same wrong number", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<LineWidget widget={makeLine(["month", "category"])} />);
    await screen.findByTestId("line-widget-unsupported");
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  // ── F5 — the regression net ───────────────────────────────────────────
  it("F5: line with ONE dimension still draws its chart", async () => {
    runQueryMock.mockResolvedValue(ONE_DIMENSION_ROWS);

    renderWithSWR(<LineWidget widget={makeLine(["month"])} />);

    const stub = await screen.findByTestId("line-chart-stub");
    expect(JSON.parse(stub.getAttribute("data-rows")!)).toEqual([
      { label: "2026-01", s0: 1000 },
      { label: "2026-02", s0: 840 },
    ]);
    expect(screen.queryByTestId("line-widget-unsupported")).toBeNull();
    expect(screen.getByTestId("widget-csv-export")).toBeInTheDocument();
  });

  // ── F6 ────────────────────────────────────────────────────────────────
  it("F6: area does NOT plot the last returned pair's value as the month's value", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<AreaWidget widget={makeArea(["month", "category"])} />);

    await screen.findByTestId("area-widget-unsupported");
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    const card = screen.getByTestId("area-widget");
    expect(card.textContent).not.toContain("900");
    expect(card.textContent).not.toContain("100");
    expect(card.textContent).not.toContain("40");
    expect(screen.queryByTestId("area-chart-stub")).toBeNull();
    expect(screen.queryByTestId("area-chart-stub-point-2026-01")).toBeNull();
  });

  // ── F7 ────────────────────────────────────────────────────────────────
  it("F7: area shows the unsupported state and NOT the chart", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<AreaWidget widget={makeArea(["month", "category"])} />);

    const notice = await screen.findByTestId("area-widget-unsupported");
    expect(notice).toHaveTextContent(SECOND_DIMENSION_UNSUPPORTED_NOTICE);
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());
    expect(screen.queryByTestId("area-chart-stub")).toBeNull();
    expect(screen.queryByTestId("area-widget-chart-loading")).toBeNull();
    expect(screen.queryByTestId("area-widget-empty")).toBeNull();
  });

  // ── F8 ────────────────────────────────────────────────────────────────
  it("F8: area PRESERVES config.dimensions — the refusal never repairs it", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);
    const widget = makeArea(["month", "category"]);
    const before = JSON.stringify(widget.config);

    renderWithSWR(<AreaWidget widget={widget} />);
    await screen.findByTestId("area-widget-unsupported");
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    expect(widget.config.dimensions).toEqual(["month", "category"]);
    expect(JSON.stringify(widget.config)).toBe(before);
  });

  // ── F9 ────────────────────────────────────────────────────────────────
  it("F9: area withholds the CSV export", async () => {
    runQueryMock.mockResolvedValue(TWO_DIMENSION_ROWS);

    renderWithSWR(<AreaWidget widget={makeArea(["month", "category"])} />);
    await screen.findByTestId("area-widget-unsupported");
    await waitFor(() => expect(runQueryMock).toHaveBeenCalled());

    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  // ── F10 — the regression net ──────────────────────────────────────────
  it("F10: area with ONE dimension still draws its chart", async () => {
    runQueryMock.mockResolvedValue(ONE_DIMENSION_ROWS);

    renderWithSWR(<AreaWidget widget={makeArea(["month"])} />);

    const stub = await screen.findByTestId("area-chart-stub");
    expect(JSON.parse(stub.getAttribute("data-rows")!)).toEqual([
      { label: "2026-01", s0: 1000 },
      { label: "2026-02", s0: 840 },
    ]);
    expect(screen.queryByTestId("area-widget-unsupported")).toBeNull();
    expect(screen.getByTestId("widget-csv-export")).toBeInTheDocument();
  });
});
