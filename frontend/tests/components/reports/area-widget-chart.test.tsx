import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * Recharts uses ResizeObserver + DOM layout measurements that don't work in
 * jsdom. Mock the chart primitives so they render their children into the DOM
 * directly — this lets us assert on the <defs>/<linearGradient> elements that
 * AreaWidgetChart emits for its gradient fills.
 */
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <svg>{children}</svg>
  ),
  AreaChart: ({ children }: { children: React.ReactNode }) => (
    <g data-testid="area-chart">{children}</g>
  ),
  Area: ({
    dataKey,
    fill,
    stroke,
    type,
  }: {
    dataKey?: string;
    fill?: string;
    stroke?: string;
    type?: string;
  }) => (
    <g
      data-testid={`area-${dataKey}`}
      data-fill={fill}
      data-stroke={stroke}
      data-type={type}
    />
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

import AreaWidgetChart from "@/components/reports/widgets/AreaWidgetChart";

const rows = [
  { label: "Jan", s0: 100, s1: 200 },
  { label: "Feb", s0: 150, s1: 250 },
];

describe("AreaWidgetChart", () => {
  it("emits a unique gradient def per series", () => {
    const { container } = render(
      <AreaWidgetChart
        widgetId="w1"
        rows={rows}
        seriesKeys={["s0", "s1"]}
        labels={["Series A", "Series B"]}
        format="number"
      />,
    );
    const grads = container.querySelectorAll("linearGradient");
    expect(grads.length).toBeGreaterThanOrEqual(2);
    // ids namespaced by widget id to avoid collisions
    expect(container.querySelector('linearGradient[id^="grad-w1-"]')).toBeTruthy();
  });

  it("namespaces gradient ids by widgetId to avoid collisions", () => {
    const { container: c1 } = render(
      <AreaWidgetChart
        widgetId="widgetA"
        rows={rows}
        seriesKeys={["s0"]}
        labels={["Series A"]}
        format="number"
      />,
    );
    const { container: c2 } = render(
      <AreaWidgetChart
        widgetId="widgetB"
        rows={rows}
        seriesKeys={["s0"]}
        labels={["Series A"]}
        format="number"
      />,
    );
    expect(c1.querySelector('linearGradient[id^="grad-widgetA-"]')).toBeTruthy();
    expect(c2.querySelector('linearGradient[id^="grad-widgetB-"]')).toBeTruthy();
    // Cross-check: widgetB id should not appear in widgetA container
    expect(c1.querySelector('linearGradient[id^="grad-widgetB-"]')).toBeNull();
  });

  it("sets fill to url(#grad-<widgetId>-<i>) on each Area", () => {
    const { container } = render(
      <AreaWidgetChart
        widgetId="w2"
        rows={rows}
        seriesKeys={["s0", "s1"]}
        labels={["Series A", "Series B"]}
        format="number"
      />,
    );
    const area0 = container.querySelector('[data-testid="area-s0"]');
    const area1 = container.querySelector('[data-testid="area-s1"]');
    expect(area0?.getAttribute("data-fill")).toBe("url(#grad-w2-0)");
    expect(area1?.getAttribute("data-fill")).toBe("url(#grad-w2-1)");
  });
});
