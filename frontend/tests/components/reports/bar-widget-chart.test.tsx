import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * Recharts uses ResizeObserver + DOM layout measurements that don't work in
 * jsdom. Mock the chart primitives so they render their children into the DOM
 * directly — this lets us assert on <Bar> props via data attributes.
 *
 * Assertion strategy: mirror the area-widget-chart test pattern. We render
 * a mocked <Bar> that writes its `radius` prop as a JSON data attribute.
 * This is more stable than asserting on SVG path `d` arc commands (which
 * depend on recharts internals and DOM layout) and directly verifies that
 * the component passes the correct radius prop to each Bar.
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

import BarWidgetChart from "@/components/reports/widgets/BarWidgetChart";

const rows = [
  { label: "Jan", value: 100 },
  { label: "Feb", value: 200 },
];

const slicedRows = [
  { label: "Jan", "s0": 100, "s1": 50 },
  { label: "Feb", "s0": 150, "s1": 80 },
];

describe("BarWidgetChart", () => {
  describe("single-series mode", () => {
    it("renders bars with rounded top corners [4,4,0,0]", () => {
      const { container } = render(
        <BarWidgetChart
          rows={rows}
          sliced={false}
          secondaryValues={[]}
          seriesKeys={[]}
          valueName="Amount"
          format="currency"
          currency="EUR"
        />,
      );
      const bar = container.querySelector('[data-testid="bar-value"]');
      expect(bar).toBeTruthy();
      expect(JSON.parse(bar!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });

    it("uses chartColor.spent (var(--color-accent)) fill for single bar", () => {
      const { container } = render(
        <BarWidgetChart
          rows={rows}
          sliced={false}
          secondaryValues={[]}
          seriesKeys={[]}
          valueName="Amount"
          format="number"
        />,
      );
      const bar = container.querySelector('[data-testid="bar-value"]');
      expect(bar?.getAttribute("data-fill")).toBe("var(--color-accent)");
    });
  });

  describe("sliced (breakdown) mode", () => {
    it("renders one Bar per secondary value", () => {
      const { container } = render(
        <BarWidgetChart
          rows={slicedRows}
          sliced={true}
          secondaryValues={["CatA", "CatB"]}
          seriesKeys={["s0", "s1"]}
          valueName="Amount"
          format="currency"
          currency="EUR"
        />,
      );
      expect(container.querySelector('[data-testid="bar-s0"]')).toBeTruthy();
      expect(container.querySelector('[data-testid="bar-s1"]')).toBeTruthy();
    });

    it("gives rounded top corners only to the topmost stacked bar", () => {
      const { container } = render(
        <BarWidgetChart
          rows={slicedRows}
          sliced={true}
          secondaryValues={["CatA", "CatB"]}
          seriesKeys={["s0", "s1"]}
          valueName="Amount"
          format="currency"
          currency="EUR"
        />,
      );
      const barS0 = container.querySelector('[data-testid="bar-s0"]');
      const barS1 = container.querySelector('[data-testid="bar-s1"]');
      // Bottom bar: radius 0 (flat top so it blends with the bar above)
      expect(JSON.parse(barS0!.getAttribute("data-radius") || "null")).toEqual(0);
      // Top bar (last): radius [4,4,0,0]
      expect(JSON.parse(barS1!.getAttribute("data-radius") || "null")).toEqual([4, 4, 0, 0]);
    });

    it("fills sliced bars with palette colors from CHART_SERIES", () => {
      const { container } = render(
        <BarWidgetChart
          rows={slicedRows}
          sliced={true}
          secondaryValues={["CatA", "CatB"]}
          seriesKeys={["s0", "s1"]}
          valueName="Amount"
          format="number"
        />,
      );
      const barS0 = container.querySelector('[data-testid="bar-s0"]');
      const barS1 = container.querySelector('[data-testid="bar-s1"]');
      // First two CHART_SERIES slots: chart-1, chart-2
      expect(barS0?.getAttribute("data-fill")).toBe("var(--color-chart-1)");
      expect(barS1?.getAttribute("data-fill")).toBe("var(--color-chart-2)");
      // The two bars must use different palette colors
      expect(barS0?.getAttribute("data-fill")).not.toBe(
        barS1?.getAttribute("data-fill"),
      );
    });
  });
});
