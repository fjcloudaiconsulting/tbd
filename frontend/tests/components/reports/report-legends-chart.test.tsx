/**
 * TBD-427: report chart legends, chart level, REAL recharts.
 *
 * ## Why real recharts and not a module stub
 *
 * `area-widget-chart.test.tsx` and `bar-widget-chart.test.tsx` replace
 * recharts wholesale with `Legend: () => null`, so a legend assertion there
 * passes whether or not the chart renders a recharts `<Legend>`: vacuous by
 * construction. Here only `ResponsiveContainer` is replaced
 * (`rechartsWithFixedSize`), so `<Legend>` renders its real
 * `.recharts-legend-wrapper` under jsdom. That was probed on the pre-fix tree,
 * where it rendered with alphabetised items coloured by series hue.
 *
 * ## What each block kills
 *
 * - L1: a recharts `<Legend>` left in (double legend, hue-coloured text,
 *   alphabetised order, no accessible name).
 * - L2: a chart ignoring the colours it is handed and recomputing
 *   `CHART_SERIES[i % 8]`, which silently desyncs chart from DOM legend. The
 *   permuted colours make an index recompute distinguishable.
 * - L5 (real widget): the real `PieWidget` through the real `next/dynamic`
 *   chart. Kills painting "Other" with `--color-border`, and a widget handing
 *   the chart a different array than the legend.
 */
import { render, waitFor } from "@testing-library/react";

import { renderChartsWithReducedMotion } from "@/tests/utils/recharts";

import { renderWithSWR, screen } from "../../utils/render-with-swr";
import { mockReportSources } from "../../utils/mock-report-sources";
import LineWidgetChart from "@/components/reports/widgets/LineWidgetChart";
import AreaWidgetChart from "@/components/reports/widgets/AreaWidgetChart";
import PieWidgetChart from "@/components/reports/widgets/PieWidgetChart";
import PieWidget from "@/components/reports/widgets/PieWidget";
import { runQuery } from "@/lib/reports/api";
import type { PieWidget as PieWidgetType } from "@/lib/reports/types";

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize();
});

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({ runQuery: vi.fn() }));

// These assertions read committed geometry, which an animating chart has not
// drawn yet. Report reduced motion so recharts renders final shapes (TBD-437).
renderChartsWithReducedMotion();

const chart = (n: number) => `var(--color-chart-${n})`;
const BORDER_STRONG = "var(--color-border-strong)";

const ROWS = [
  { label: "Jan", s0: 100, s1: 200 },
  { label: "Feb", s0: 150, s1: 250 },
];
const PIE_ROWS = [
  { label: "Rent", value: 5 },
  { label: "Food", value: 3 },
  { label: "Other", value: 2 },
];

async function mounted(container: HTMLElement, selector: string) {
  await waitFor(() => expect(container.querySelector(selector)).not.toBeNull());
}

function rechartsLegend(container: HTMLElement) {
  return (
    container.querySelector(".recharts-legend-wrapper") ??
    container.querySelector(".recharts-default-legend")
  );
}

describe("L1: no recharts <Legend> in the report charts", () => {
  it("line, two series", async () => {
    const { container } = render(
      <LineWidgetChart
        rows={ROWS}
        seriesKeys={["s0", "s1"]}
        labels={["Zeta", "Alpha"]}
        seriesColors={[chart(1), chart(2)]}
        format="number"
      />,
    );
    await mounted(container, ".recharts-line-curve");
    expect(rechartsLegend(container)).toBeNull();
  });

  it.each([
    ["overlaid", undefined],
    ["stacked", "stack"],
  ])("area, two series, %s", async (_name, stackId) => {
    const { container } = render(
      <AreaWidgetChart
        rows={ROWS}
        seriesKeys={["s0", "s1"]}
        labels={["Zeta", "Alpha"]}
        seriesColors={[chart(1), chart(2)]}
        stackId={stackId}
        format="number"
        widgetId="w"
      />,
    );
    await mounted(container, ".recharts-area-curve");
    expect(rechartsLegend(container)).toBeNull();
  });

  it("pie, three slices", async () => {
    const { container } = render(
      <PieWidgetChart
        rows={PIE_ROWS}
        sliceColors={[chart(1), chart(2), BORDER_STRONG]}
        format="number"
      />,
    );
    await mounted(container, ".recharts-pie-sector");
    expect(rechartsLegend(container)).toBeNull();
  });
});

describe("L2: each chart paints exactly the colours it is handed", () => {
  it("line strokes", async () => {
    const { container } = render(
      <LineWidgetChart
        rows={ROWS}
        seriesKeys={["s0", "s1"]}
        labels={["Zeta", "Alpha"]}
        seriesColors={[chart(5), chart(3)]}
        format="number"
      />,
    );
    await mounted(container, ".recharts-line-curve");
    const strokes = [...container.querySelectorAll(".recharts-line-curve")].map(
      (el) => el.getAttribute("stroke"),
    );
    expect(strokes).toEqual([chart(5), chart(3)]);
  });

  it("area strokes and gradient stops", async () => {
    const { container } = render(
      <AreaWidgetChart
        rows={ROWS}
        seriesKeys={["s0", "s1"]}
        labels={["Zeta", "Alpha"]}
        seriesColors={[chart(5), chart(3)]}
        format="number"
        widgetId="w"
      />,
    );
    await mounted(container, ".recharts-area-curve");
    const strokes = [...container.querySelectorAll(".recharts-area-curve")].map(
      (el) => el.getAttribute("stroke"),
    );
    expect(strokes).toEqual([chart(5), chart(3)]);
    const stops = [...container.querySelectorAll("linearGradient")].map((g) =>
      [...g.querySelectorAll("stop")].map((s) => s.getAttribute("stop-color")),
    );
    expect(stops).toEqual([
      [chart(5), chart(5)],
      [chart(3), chart(3)],
    ]);
  });

  it("pie sector fills", async () => {
    const { container } = render(
      <PieWidgetChart
        rows={PIE_ROWS}
        sliceColors={[chart(4), chart(1), BORDER_STRONG]}
        format="number"
      />,
    );
    await mounted(container, ".recharts-pie-sector");
    const fills = [...container.querySelectorAll(".recharts-pie-sector path")].map(
      (el) => el.getAttribute("fill"),
    );
    expect(fills).toEqual([chart(4), chart(1), BORDER_STRONG]);
  });
});

describe("L5: the real PieWidget paints Other border-strong, chart and legend alike", () => {
  it("sector fills equal the legend swatches, Other last in border-strong", async () => {
    vi.mocked(runQuery).mockResolvedValue({
      rows: [
        { category: "Rent", value: 5 },
        { category: "Food", value: 3 },
        { category: "Misc", value: 1 },
        { category: "Fun", value: 1 },
      ],
      meta: { row_count: 4, truncated: false, query_ms: 1 },
    });
    const widget: PieWidgetType = {
      id: "w_pie",
      type: "pie",
      title: "Spend share",
      grid: { x: 0, y: 0, w: 4, h: 4 },
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        dimensions: ["category"],
        sort: { by: "value", dir: "desc" },
        limit: 50,
        top_n: 2,
      },
    };
    const { container } = renderWithSWR(<PieWidget widget={widget} />);
    await mounted(container, ".recharts-pie-sector");

    const fills = [...container.querySelectorAll(".recharts-pie-sector path")].map(
      (el) => el.getAttribute("fill"),
    );
    expect(fills).toHaveLength(3);
    expect(fills[2]).toBe(BORDER_STRONG);

    const swatches = screen
      .getAllByTestId("pie-widget-legend-swatch")
      .map((el) => el.getAttribute("data-color"));
    expect(swatches).toEqual(fills);
  });
});

describe("L13 (chart half): duplicate slice labels are not duplicate keys", () => {
  // KILLS: `<Cell key={row.label}>`. `topNWithOther` appends a literal
  // "Other", so a real "Other" category in the top N duplicates the label.
  it("a real 'Other' beside the folded 'Other' emits no duplicate-key error", async () => {
    const errors = vi.spyOn(console, "error");
    const { container } = render(
      <PieWidgetChart
        rows={[
          { label: "Other", value: 10 },
          { label: "Rent", value: 5 },
          { label: "Other", value: 4 },
        ]}
        sliceColors={[chart(1), chart(2), BORDER_STRONG]}
        format="number"
      />,
    );
    await mounted(container, ".recharts-pie-sector");
    expect(container.querySelectorAll(".recharts-pie-sector")).toHaveLength(3);
    const duplicateKey = errors.mock.calls.filter((call) =>
      call.some((arg) => String(arg).includes("same key")),
    );
    expect(duplicateKey).toEqual([]);
    errors.mockRestore();
  });
});
