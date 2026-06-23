import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * Recharts uses ResizeObserver + DOM layout measurements that don't work in
 * jsdom. Mock the chart primitives so they render their children into the DOM
 * directly — this lets us assert on <Bar> props via data attributes.
 *
 * Assertion strategy: mirror the bar-widget-chart test pattern. We render a
 * mocked <Bar> that writes its `radius` prop as a `data-radius` attribute so
 * we can verify the stacked-radius ternary without depending on SVG output.
 */
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <svg>{children}</svg>
  ),
  BarChart: ({ children }: { children: React.ReactNode }) => (
    <g data-testid="bar-chart">{children}</g>
  ),
  Bar: ({
    dataKey,
    fill,
    radius,
    stackId,
  }: {
    dataKey?: string;
    fill?: string;
    radius?: number | number[];
    stackId?: string;
  }) => (
    <g
      data-testid={`bar-${dataKey}`}
      data-fill={fill}
      data-radius={JSON.stringify(radius)}
      data-stack-id={stackId}
    />
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

import StackedBarWidgetChart from "@/components/reports/widgets/StackedBarWidgetChart";

const rows = [
  { label: "Jan", s0: 100, s1: 50, s2: 30 },
  { label: "Feb", s0: 150, s1: 80, s2: 20 },
];

describe("StackedBarWidgetChart — stacked-radius ternary", () => {
  describe("when stacked (stackId provided, multiple series)", () => {
    it("gives radius 0 to all bars except the last", () => {
      const { container } = render(
        <StackedBarWidgetChart
          rows={rows}
          seriesKeys={["s0", "s1", "s2"]}
          labels={["A", "B", "C"]}
          stackId="stack"
          format="number"
        />,
      );
      const bar0 = container.querySelector('[data-testid="bar-s0"]');
      const bar1 = container.querySelector('[data-testid="bar-s1"]');
      expect(JSON.parse(bar0!.getAttribute("data-radius") || "null")).toEqual(0);
      expect(JSON.parse(bar1!.getAttribute("data-radius") || "null")).toEqual(0);
    });

    it("gives radius [4,4,0,0] only to the last (top) bar", () => {
      const { container } = render(
        <StackedBarWidgetChart
          rows={rows}
          seriesKeys={["s0", "s1", "s2"]}
          labels={["A", "B", "C"]}
          stackId="stack"
          format="number"
        />,
      );
      const barLast = container.querySelector('[data-testid="bar-s2"]');
      expect(JSON.parse(barLast!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });

    it("works correctly with two series (bottom=0, top=[4,4,0,0])", () => {
      const twoRows = [{ label: "Jan", a: 100, b: 50 }];
      const { container } = render(
        <StackedBarWidgetChart
          rows={twoRows}
          seriesKeys={["a", "b"]}
          labels={["Series A", "Series B"]}
          stackId="stack"
          format="currency"
          currency="EUR"
        />,
      );
      expect(JSON.parse(container.querySelector('[data-testid="bar-a"]')!.getAttribute("data-radius") || "null")).toEqual(0);
      expect(JSON.parse(container.querySelector('[data-testid="bar-b"]')!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });
  });

  describe("when NOT stacked (no stackId)", () => {
    it("gives radius [4,4,0,0] to every bar", () => {
      const { container } = render(
        <StackedBarWidgetChart
          rows={rows}
          seriesKeys={["s0", "s1", "s2"]}
          labels={["A", "B", "C"]}
          format="number"
        />,
      );
      const bar0 = container.querySelector('[data-testid="bar-s0"]');
      const bar1 = container.querySelector('[data-testid="bar-s1"]');
      const bar2 = container.querySelector('[data-testid="bar-s2"]');
      expect(JSON.parse(bar0!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
      expect(JSON.parse(bar1!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
      expect(JSON.parse(bar2!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });

    it("gives radius [4,4,0,0] to single series bar", () => {
      const singleRow = [{ label: "Jan", s0: 100 }];
      const { container } = render(
        <StackedBarWidgetChart
          rows={singleRow}
          seriesKeys={["s0"]}
          labels={["Series A"]}
          format="currency"
          currency="EUR"
        />,
      );
      const bar = container.querySelector('[data-testid="bar-s0"]');
      expect(JSON.parse(bar!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });
  });
});
