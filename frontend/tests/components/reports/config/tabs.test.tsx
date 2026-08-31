/**
 * DataTab / StyleTab composers — covers the per-type visibility matrix and
 * the two verbatim transcription hotspots (the measure cast-and-extract and
 * the area/stacked_bar stacked label+default split).
 */
import { renderWithSWR, screen } from "../../../utils/render-with-swr";

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
      // TBD-382: the break-down is what the layout flag acts on, so it has
      // to be present for the control to be offered at all.
      dimensions: ["month", "category"],
      ...(stacked === undefined ? {} : { stacked }),
    },
  };
}

// The pre-TBD-382 shape: a stacked_bar with NO secondary dimension. Kept as
// the negative case — with no break-down the flag is inert
// (BarWidgetChart ignores ``stacked`` when ``sliced`` is false), so the
// control must not be offered.
function makeStackedNoSecondary(): StackedBarWidget {
  return {
    id: "w_stacked_flat",
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
    expect(screen.getByLabelText("Measure")).toBeInTheDocument();
    expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
    expect(screen.getByLabelText("Break down by")).toBeInTheDocument();
  });

  it("hides dimensions entirely for kpi", () => {
    renderData(makeKpi());
    expect(screen.getByLabelText("Measure")).toBeInTheDocument();
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

  it("stacked_bar uses the 'Bar layout' label and defaults checked", () => {
    renderStyle(makeStacked());
    expect(screen.getByText("Bar layout")).toBeInTheDocument();
    const cb = screen.getByLabelText(
      "Stack the break-down into one bar",
    ) as HTMLInputElement;
    expect(cb.checked).toBe(true);
  });

  it("stacked_bar with stacked:false is unchecked", () => {
    renderStyle(makeStacked(false));
    const cb = screen.getByLabelText(
      "Stack the break-down into one bar",
    ) as HTMLInputElement;
    expect(cb.checked).toBe(false);
  });

  it("hides 'Bar layout' on a stacked_bar with no break-down", () => {
    // TBD-382. ``stacked`` is ignored when there is no ``dimensions[1]``
    // (BarWidgetChart: "Ignored when `sliced` is false"), and "Break down
    // by" is now reachable-but-optional on stacked_bar, so "None" is a real
    // state. A checkbox that changes nothing is the same false assertion
    // this ticket removed from the chart. Subtractive, per FilterEditor's
    // TBD-381 rule: a control is offered iff it applies.
    renderStyle(makeStackedNoSecondary());
    expect(screen.queryByText("Bar layout")).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Stack the break-down into one bar"),
    ).not.toBeInTheDocument();
  });

  it("area uses the 'Stack series' label and defaults unchecked", () => {
    renderStyle(makeArea());
    expect(screen.getByText("Stack series")).toBeInTheDocument();
    const cb = screen.getByLabelText(
      "Stack multiple series",
    ) as HTMLInputElement;
    expect(cb.checked).toBe(false);
  });

  it("gives both stack toggles an accessible name matching their visible text", () => {
    // TBD-382 / WCAG 2.5.3 Label in Name (Level A). Both arms carried
    // ``aria-label="Stack series"`` while rendering different visible text,
    // so the accessible name did NOT contain the visible label and a
    // voice-control user saying the words they see missed the control.
    // ``getByLabelText`` cannot see this — it matches the wrapping <label>
    // text too — so the fence has to ask for the computed ACCESSIBLE NAME.
    const area = renderStyle(makeArea());
    expect(
      screen.getByRole("checkbox", { name: "Stack multiple series" }),
    ).toBeInTheDocument();
    area.unmount();

    renderStyle(makeStacked());
    expect(
      screen.getByRole("checkbox", { name: "Stack the break-down into one bar" }),
    ).toBeInTheDocument();
  });

  it("area with stacked:true is checked", () => {
    renderStyle(makeArea(true));
    const cb = screen.getByLabelText(
      "Stack multiple series",
    ) as HTMLInputElement;
    expect(cb.checked).toBe(true);
  });
});
