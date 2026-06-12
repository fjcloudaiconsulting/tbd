/**
 * The extracted mutation closures must produce the SAME onUpdate payloads
 * they did inline in ConfigRail. Renders a tiny harness that calls the
 * setters and records every payload. ``buildWidgetMutations`` is a plain
 * factory (no React hooks) — the harness invokes it unconditionally per
 * render, exactly like every real caller.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import { buildWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type {
  AreaWidget,
  BarWidget,
  KPIWidget,
  LineWidget,
  PieWidget,
  StackedBarWidget,
  Widget,
} from "@/lib/reports/types";

function Harness({
  widget,
  onUpdate,
}: {
  widget: Widget;
  onUpdate: (next: Widget) => void;
}) {
  const m = buildWidgetMutations(widget, onUpdate);
  return (
    <div>
      <button onClick={() => m.setTitle("Renamed")}>title</button>
      <button onClick={() => m.setPrimaryDimension("account")}>set-primary</button>
      <button onClick={() => m.setSecondaryDimension("")}>clear-secondary</button>
      <button onClick={() => m.setSecondaryDimension("account")}>set-secondary</button>
      <button onClick={() => m.setComparePrior(true)}>compare-prior</button>
      <button onClick={() => m.setTopN(12)}>top-n</button>
      <button onClick={() => m.setStacked(true)}>stacked</button>
      <button onClick={() => m.setSingleMeasure({ agg: "count", field: "id" })}>
        single-measure
      </button>
      <button
        onClick={() =>
          m.setSeries([
            { measure: { agg: "sum", field: "amount" } },
            { measure: { agg: "avg", field: "amount" } },
          ])
        }
      >
        set-series
      </button>
    </div>
  );
}

function makeBar(dimensions: BarWidget["config"]["dimensions"] = ["category", "account"]): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions,
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

function renderHarness(widget: Widget) {
  const updates: Widget[] = [];
  renderWithSWR(<Harness widget={widget} onUpdate={(w) => updates.push(w)} />);
  return updates;
}

describe("buildWidgetMutations", () => {
  it("setTitle replaces the widget title", () => {
    const updates = renderHarness(makeBar());
    fireEvent.click(screen.getByText("title"));
    expect(updates.at(-1)?.title).toBe("Renamed");
  });

  it("setPrimaryDimension overwrites dimensions[0]", () => {
    const updates = renderHarness(makeBar(["category", "tag"]));
    fireEvent.click(screen.getByText("set-primary"));
    expect((updates.at(-1) as BarWidget).config.dimensions).toEqual([
      "account",
      "tag",
    ]);
  });

  it("setSecondaryDimension('') splices dimensions[1]", () => {
    const updates = renderHarness(makeBar());
    fireEvent.click(screen.getByText("clear-secondary"));
    expect((updates.at(-1) as BarWidget).config.dimensions).toEqual(["category"]);
  });

  it("setSecondaryDimension('account') sets dimensions[1]", () => {
    const updates = renderHarness(makeBar(["category"]));
    fireEvent.click(screen.getByText("set-secondary"));
    expect((updates.at(-1) as BarWidget).config.dimensions).toEqual([
      "category",
      "account",
    ]);
  });

  it("setComparePrior toggles the KPI compare flag", () => {
    const updates = renderHarness(makeKpi());
    fireEvent.click(screen.getByText("compare-prior"));
    expect((updates.at(-1) as KPIWidget).config.compare_prior_period).toBe(true);
  });

  it("setComparePrior is a no-op on a non-KPI widget", () => {
    const updates = renderHarness(makeBar());
    fireEvent.click(screen.getByText("compare-prior"));
    expect(updates).toHaveLength(0);
  });

  it("setTopN sets the pie top_n", () => {
    const updates = renderHarness(makePie());
    fireEvent.click(screen.getByText("top-n"));
    expect((updates.at(-1) as PieWidget).config.top_n).toBe(12);
  });

  it("setStacked sets the stacked flag on area", () => {
    const updates = renderHarness(makeArea());
    fireEvent.click(screen.getByText("stacked"));
    expect((updates.at(-1) as AreaWidget).config.stacked).toBe(true);
  });

  it("setStacked sets the stacked flag on stacked_bar", () => {
    const updates = renderHarness(makeStacked());
    fireEvent.click(screen.getByText("stacked"));
    expect((updates.at(-1) as StackedBarWidget).config.stacked).toBe(true);
  });

  it("setSingleMeasure replaces the measure on a single-series widget", () => {
    const updates = renderHarness(makeBar());
    fireEvent.click(screen.getByText("single-measure"));
    expect((updates.at(-1) as BarWidget).config.measure).toEqual({
      agg: "count",
      field: "id",
    });
  });

  it("setSingleMeasure is a no-op on a multi-series widget", () => {
    const updates = renderHarness(makeLine());
    fireEvent.click(screen.getByText("single-measure"));
    expect(updates).toHaveLength(0);
  });

  it("setSeries replaces the measures on a multi-series widget", () => {
    const updates = renderHarness(makeLine());
    fireEvent.click(screen.getByText("set-series"));
    expect((updates.at(-1) as LineWidget).config.measures).toEqual([
      { measure: { agg: "sum", field: "amount" } },
      { measure: { agg: "avg", field: "amount" } },
    ]);
  });

  it("setSeries is a no-op on a single-series widget", () => {
    const updates = renderHarness(makeBar());
    fireEvent.click(screen.getByText("set-series"));
    expect(updates).toHaveLength(0);
  });
});
