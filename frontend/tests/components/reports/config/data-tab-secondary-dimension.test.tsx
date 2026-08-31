/**
 * Secondary dimension picker visibility (re-homed from the original
 * rail's secondary-dimension test onto the extracted ``DataTab``, which
 * owns the primary/secondary dimension selects and their per-type
 * visibility).
 *
 * The "Secondary dimension" picker is Table-only. The bar widget exposes
 * a "Break down by" picker that slices each total bar into stacked,
 * per-secondary-value colored segments (e.g. per account). line / area /
 * kpi / pie / sparkline still only consume ``dimensions[0]``, so they
 * expose no secondary picker.
 *
 * ⚠ TBD-382: ``stacked_bar`` USED TO BE in HIDDEN_CASES below. That fixture
 * actively certified Defect A — a stacked bar's only stacking axis is now
 * ``dimensions[1]``, and gating the control to ``bar || table`` was what
 * made the axis unreachable from the editor while templates set it anyway.
 * It moves here (F10) rather than being deleted, so the gap it certified
 * cannot quietly reopen.
 */
import { mockReportSources } from "../../../utils/mock-report-sources";
import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import { apiFetch } from "@/lib/api";
import type {
  AreaWidget,
  BarWidget,
  KPIWidget,
  LineWidget,
  PieWidget,
  SparklineWidget,
  StackedBarWidget,
  TableWidget,
  Widget,
} from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(() =>
    Promise.resolve([]) as Promise<unknown>,
  );
});

function makeBar(): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function makeLine(): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function makeArea(): AreaWidget {
  return {
    id: "w_area",
    type: "area",
    title: "Area",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function makeStacked(): StackedBarWidget {
  return {
    id: "w_stacked",
    type: "stacked_bar",
    title: "Stacked",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function makeKpi(): KPIWidget {
  return {
    id: "w_kpi",
    type: "kpi",
    title: "KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
    },
  };
}

function makePie(): PieWidget {
  return {
    id: "w_pie",
    type: "pie",
    title: "Pie",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function makeSparkline(): SparklineWidget {
  return {
    id: "w_spark",
    type: "sparkline",
    title: "Spark",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
    },
  };
}

function makeTable(): TableWidget {
  return {
    id: "w_table",
    type: "table",
    title: "Table",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["category"],
    },
  };
}

const HIDDEN_CASES: Array<{ name: string; widget: Widget }> = [
  { name: "line", widget: makeLine() },
  { name: "area", widget: makeArea() },
  { name: "kpi", widget: makeKpi() },
  { name: "pie", widget: makePie() },
  { name: "sparkline", widget: makeSparkline() },
];

describe("DataTab — secondary dimension picker visibility", () => {
  for (const { name, widget } of HIDDEN_CASES) {
    it(`does NOT render the secondary dimension picker for ${name}`, () => {
      renderWithSWR(<DataTab widget={widget} onUpdate={() => {}} />);
      expect(
        screen.queryByLabelText("Secondary dimension"),
      ).not.toBeInTheDocument();
    });
  }

  it("DOES render the secondary dimension picker for table", () => {
    renderWithSWR(<DataTab widget={makeTable()} onUpdate={() => {}} />);
    expect(
      screen.getByLabelText("Secondary dimension"),
    ).toBeInTheDocument();
  });

  it("DOES render the 'Break down by' picker for bar", () => {
    renderWithSWR(<DataTab widget={makeBar()} onUpdate={() => {}} />);
    expect(screen.getByLabelText("Break down by")).toBeInTheDocument();
    // The bar picker is NOT labeled "Secondary dimension" (table-only).
    expect(
      screen.queryByLabelText("Secondary dimension"),
    ).not.toBeInTheDocument();
  });

  it("sets dimensions[1] when a bar break-down is chosen, and clears it on None", () => {
    const updates: Widget[] = [];
    const bar = makeBar();
    const { rerender } = renderWithSWR(
      <DataTab widget={bar} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Break down by"), {
      target: { value: "account" },
    });
    const afterSet = updates.at(-1) as BarWidget;
    expect(afterSet.config.dimensions).toEqual(["category", "account"]);

    rerender(<DataTab widget={afterSet} onUpdate={(w) => updates.push(w)} />);

    fireEvent.change(screen.getByLabelText("Break down by"), {
      target: { value: "" },
    });
    const afterClear = updates.at(-1) as BarWidget;
    expect(afterClear.config.dimensions).toEqual(["category"]);
  });

  // ── F10 ─────────────────────────────────────────────────────────────
  it("F10: DOES render the 'Break down by' picker for stacked_bar", () => {
    renderWithSWR(<DataTab widget={makeStacked()} onUpdate={() => {}} />);
    expect(screen.getByLabelText("Break down by")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Secondary dimension"),
    ).not.toBeInTheDocument();
  });

  it("F10: sets dimensions[1] on a stacked_bar break-down, and clears it on None", () => {
    const updates: Widget[] = [];
    const stacked = makeStacked();
    const { rerender } = renderWithSWR(
      <DataTab widget={stacked} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Break down by"), {
      target: { value: "category" },
    });
    const afterSet = updates.at(-1) as StackedBarWidget;
    expect(afterSet.config.dimensions).toEqual(["month", "category"]);

    rerender(<DataTab widget={afterSet} onUpdate={(w) => updates.push(w)} />);

    fireEvent.change(screen.getByLabelText("Break down by"), {
      target: { value: "" },
    });
    const afterClear = updates.at(-1) as StackedBarWidget;
    expect(afterClear.config.dimensions).toEqual(["month"]);
  });
});

// ── F11 ───────────────────────────────────────────────────────────────
describe("F11: stacked_bar is a SINGLE-measure widget that still persists `measures`", () => {
  it("renders one measure editor and no add-series control", () => {
    renderWithSWR(<DataTab widget={makeStacked()} onUpdate={() => {}} />);
    expect(screen.getByLabelText("Measure")).toBeInTheDocument();
    expect(screen.queryByTestId("measure-add")).not.toBeInTheDocument();
  });

  it("writes back config.measures[0] — a length-1 array, and NEVER a singular `measure` key", async () => {
    // ⚠ The implementation trap is flipping `isMultiSeries`, which is also
    // the type guard `setSeries` early-returns on: flipping it routes the
    // write to `setSingleMeasure` and `config.measure`, which is a missing
    // required field on the backend's `_MultiSeriesConfig` (min_length=1)
    // and 422s on the next save — on BOTH the reports and dashboard paths.
    // TBD-402: the measure select is catalog-driven, so this test needs the
    // real catalog. With an empty one the control is deliberately DISABLED —
    // there is no list to pick from, and offering a stale fallback is how an
    // invalid pair got chosen before.
    vi.mocked(apiFetch).mockImplementation(
      mockReportSources() as unknown as typeof apiFetch,
    );
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeStacked()} onUpdate={(w) => updates.push(w)} />,
    );

    const select = screen.getByLabelText("Measure") as HTMLSelectElement;
    await waitFor(() => expect(select.disabled).toBe(false));
    // The catalog KEY, not an agg: `avg_amount` is transactions' avg(amount).
    fireEvent.change(select, { target: { value: "avg_amount" } });

    const next = updates.at(-1) as StackedBarWidget;
    expect(next.config.measures).toHaveLength(1);
    expect(next.config.measures[0].measure).toEqual({
      agg: "avg",
      field: "amount",
    });
    expect(Object.keys(next.config)).not.toContain("measure");
  });
});

describe("DataTab — measure editor + dimension by widget type", () => {
  // Multi-series types render the MeasuresEditor (with the add button);
  // single-measure types render SingleMeasureEditor (no add button).
  const MULTI: Array<{ name: string; widget: Widget }> = [
    { name: "line", widget: makeLine() },
    { name: "area", widget: makeArea() },
    { name: "table", widget: makeTable() },
  ];

  for (const { name, widget } of MULTI) {
    it(`renders the multi-series MeasuresEditor for ${name}`, () => {
      renderWithSWR(<DataTab widget={widget} onUpdate={() => {}} />);
      expect(screen.getByTestId("measure-add")).toBeInTheDocument();
    });
  }

  it("renders a single measure editor (no add button) for bar", () => {
    renderWithSWR(<DataTab widget={makeBar()} onUpdate={() => {}} />);
    expect(screen.getByLabelText("Measure")).toBeInTheDocument();
    expect(screen.queryByTestId("measure-add")).not.toBeInTheDocument();
  });

  it("renders no dimension control for kpi", () => {
    renderWithSWR(<DataTab widget={makeKpi()} onUpdate={() => {}} />);
    expect(
      screen.queryByLabelText("Primary dimension"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Secondary dimension"),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Break down by")).not.toBeInTheDocument();
  });

  for (const { name, widget } of [
    { name: "pie", widget: makePie() },
    { name: "sparkline", widget: makeSparkline() },
  ]) {
    it(`renders only a single (primary) dimension for ${name}`, () => {
      renderWithSWR(<DataTab widget={widget} onUpdate={() => {}} />);
      expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
      expect(
        screen.queryByLabelText("Secondary dimension"),
      ).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Break down by")).not.toBeInTheDocument();
      // single-measure: no add-series button
      expect(screen.queryByTestId("measure-add")).not.toBeInTheDocument();
    });
  }
});
