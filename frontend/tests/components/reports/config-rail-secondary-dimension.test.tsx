/**
 * Regression: the "Secondary dimension" picker is Table-only in v1.
 * Bar / line / area / stacked-bar / kpi / pie / sparkline widgets
 * only consume ``dimensions[0]``, so exposing the picker for them
 * would be a no-op UX. Architect-locked Option A — split-series
 * rendering is a follow-up if users ask for it.
 */
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";

import ConfigRail from "@/components/reports/ConfigRail";
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

function renderIsolated(ui: React.ReactElement) {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      {ui}
    </SWRConfig>,
  );
}

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
  { name: "bar", widget: makeBar() },
  { name: "line", widget: makeLine() },
  { name: "area", widget: makeArea() },
  { name: "stacked_bar", widget: makeStacked() },
  { name: "kpi", widget: makeKpi() },
  { name: "pie", widget: makePie() },
  { name: "sparkline", widget: makeSparkline() },
];

describe("ConfigRail — secondary dimension picker visibility", () => {
  for (const { name, widget } of HIDDEN_CASES) {
    it(`does NOT render the secondary dimension picker for ${name}`, () => {
      renderIsolated(
        <ConfigRail
          widget={widget}
          canvasFilters={{}}
          onUpdate={() => {}}
          onClose={() => {}}
        />,
      );
      expect(
        screen.queryByLabelText("Secondary dimension"),
      ).not.toBeInTheDocument();
    });
  }

  it("DOES render the secondary dimension picker for table", () => {
    renderIsolated(
      <ConfigRail
        widget={makeTable()}
        canvasFilters={{}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(
      screen.getByLabelText("Secondary dimension"),
    ).toBeInTheDocument();
  });
});
