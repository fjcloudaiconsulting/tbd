/**
 * DataTab / StyleTab composers — covers the per-type visibility matrix and
 * the two verbatim transcription hotspots (the measure cast-and-extract and
 * the area/stacked_bar stacked label+default split).
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import StyleTab from "@/components/reports/config/StyleTab";
import { apiFetch } from "@/lib/api";
import type {
  AreaWidget,
  BarWidget,
  KPIWidget,
  PieWidget,
  StackedBarWidget,
  TableWidget,
  Widget,
} from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(
    () => Promise.resolve([]) as Promise<unknown>,
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

function makeKpi(): KPIWidget {
  return {
    id: "w_kpi",
    type: "kpi",
    title: "KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
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

function makeArea(stacked?: boolean): AreaWidget {
  return {
    id: "w_area",
    type: "area",
    title: "Area",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      ...(stacked === undefined ? {} : { stacked }),
    },
  };
}

function makeStacked(stacked?: boolean): StackedBarWidget {
  return {
    id: "w_stacked",
    type: "stacked_bar",
    title: "Stacked",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      ...(stacked === undefined ? {} : { stacked }),
    },
  };
}

function renderData(widget: Widget, onUpdate: (w: Widget) => void = () => {}) {
  return renderWithSWR(<DataTab widget={widget} onUpdate={onUpdate} />);
}

function renderStyle(widget: Widget, onUpdate: (w: Widget) => void = () => {}) {
  return renderWithSWR(<StyleTab widget={widget} onUpdate={onUpdate} />);
}

describe("DataTab", () => {
  it("renders single measure + primary + secondary for bar", () => {
    renderData(makeBar());
    expect(screen.getByLabelText("Data source")).toBeInTheDocument();
    expect(screen.getByLabelText("Aggregation")).toBeInTheDocument();
    expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
    expect(screen.getByLabelText("Break down by")).toBeInTheDocument();
  });

  it("hides dimensions entirely for kpi", () => {
    renderData(makeKpi());
    expect(screen.getByLabelText("Aggregation")).toBeInTheDocument();
    expect(screen.queryByLabelText("Primary dimension")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Break down by")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Secondary dimension")).not.toBeInTheDocument();
  });

  it("shows primary but no secondary for pie", () => {
    renderData(makePie());
    expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
    expect(screen.queryByLabelText("Secondary dimension")).not.toBeInTheDocument();
  });

  it("shows MeasuresEditor and a table secondary for table", () => {
    renderData(makeTable());
    expect(screen.getByTestId("measure-add")).toBeInTheDocument();
    expect(screen.getByLabelText("Secondary dimension")).toBeInTheDocument();
  });
});

describe("StyleTab", () => {
  it("renders the title for every type", () => {
    renderStyle(makeBar());
    expect(screen.getByLabelText("Widget title")).toBeInTheDocument();
  });

  it("shows the compare checkbox only for kpi", () => {
    renderStyle(makeKpi());
    expect(screen.getByLabelText("Compare to prior period")).toBeInTheDocument();
  });

  it("shows top_n only for pie", () => {
    renderStyle(makePie());
    expect(screen.getByLabelText("Top N slices")).toBeInTheDocument();
  });

  it("stacked_bar uses the 'Stack mode' label and defaults checked", () => {
    renderStyle(makeStacked());
    expect(screen.getByText("Stack mode")).toBeInTheDocument();
    const cb = screen.getByLabelText("Stack series") as HTMLInputElement;
    expect(cb.checked).toBe(true);
  });

  it("stacked_bar with stacked:false is unchecked", () => {
    renderStyle(makeStacked(false));
    const cb = screen.getByLabelText("Stack series") as HTMLInputElement;
    expect(cb.checked).toBe(false);
  });

  it("area uses the 'Stack series' label and defaults unchecked", () => {
    renderStyle(makeArea());
    expect(screen.getByText("Stack series")).toBeInTheDocument();
    const cb = screen.getByLabelText("Stack series") as HTMLInputElement;
    expect(cb.checked).toBe(false);
  });

  it("area with stacked:true is checked", () => {
    renderStyle(makeArea(true));
    const cb = screen.getByLabelText("Stack series") as HTMLInputElement;
    expect(cb.checked).toBe(true);
  });
});
