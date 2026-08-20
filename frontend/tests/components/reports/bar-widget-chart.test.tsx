import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * Recharts uses ResizeObserver + DOM layout measurements that don't work in
 * jsdom. Mock the chart primitives so they render their children into the DOM
 * directly — this lets us assert on <Bar> props via data attributes.
 *
 * Assertion strategy: mirror the area-widget-chart test pattern. We render
 * a mocked <Bar> that writes its props as data attributes. This is more
 * stable than asserting on SVG path `d` arc commands (which depend on
 * recharts internals and DOM layout) and directly verifies what the
 * component hands each Bar.
 *
 * TBD-382 adds F5 (the `stacked` boolean flips `stackId`), F27 (reduced
 * motion) and F28 (the 1.4.11 segment separator).
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
    stroke,
    strokeWidth,
    isAnimationActive,
    animationDuration,
  }: {
    dataKey?: string;
    fill?: string;
    radius?: number | number[];
    stackId?: string;
    stroke?: string;
    strokeWidth?: number;
    isAnimationActive?: boolean;
    animationDuration?: number;
  }) => (
    <g
      data-testid={`bar-${dataKey}`}
      data-fill={fill}
      data-radius={JSON.stringify(radius)}
      data-stack-id={stackId === undefined ? "undefined" : stackId}
      data-stroke={stroke}
      data-stroke-width={String(strokeWidth)}
      data-is-animation-active={String(isAnimationActive)}
      data-animation-duration={String(animationDuration)}
    />
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

import BarWidgetChart from "@/components/reports/widgets/BarWidgetChart";
import { CHART_SERIES } from "@/lib/chart-colors";

const rows = [
  { label: "Jan", value: 100 },
  { label: "Feb", value: 200 },
];

const slicedRows = [
  { label: "Jan", s0: 100, s1: 50 },
  { label: "Feb", s0: 150, s1: 80 },
];

const TWO_COLORS = [CHART_SERIES[0], CHART_SERIES[1]];

function renderSliced(
  overrides: Partial<React.ComponentProps<typeof BarWidgetChart>> = {},
) {
  return render(
    <BarWidgetChart
      rows={slicedRows}
      sliced
      stacked
      secondaryValues={["CatA", "CatB"]}
      seriesKeys={["s0", "s1"]}
      sliceColors={TWO_COLORS}
      valueName="Amount"
      format="currency"
      currency="EUR"
      {...overrides}
    />,
  );
}

describe("BarWidgetChart", () => {
  describe("single-series mode", () => {
    it("renders bars with rounded top corners [4,4,0,0]", () => {
      const { container } = render(
        <BarWidgetChart
          rows={rows}
          sliced={false}
          stacked
          secondaryValues={[]}
          seriesKeys={[]}
          sliceColors={[]}
          valueName="Amount"
          format="currency"
          currency="EUR"
        />,
      );
      const bar = container.querySelector('[data-testid="bar-value"]');
      expect(bar).toBeTruthy();
      expect(JSON.parse(bar!.getAttribute("data-radius") || "null")).toEqual([
        4, 4, 0, 0,
      ]);
    });

    it("uses chartColor.spent (var(--color-accent)) fill for single bar", () => {
      const { container } = render(
        <BarWidgetChart
          rows={rows}
          sliced={false}
          stacked
          secondaryValues={[]}
          seriesKeys={[]}
          sliceColors={[]}
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
      const { container } = renderSliced();
      expect(container.querySelector('[data-testid="bar-s0"]')).toBeTruthy();
      expect(container.querySelector('[data-testid="bar-s1"]')).toBeTruthy();
    });

    it("gives rounded top corners only to the topmost stacked bar", () => {
      const { container } = renderSliced();
      const barS0 = container.querySelector('[data-testid="bar-s0"]');
      const barS1 = container.querySelector('[data-testid="bar-s1"]');
      // Bottom bar: radius 0 (flat top so it blends with the bar above)
      expect(JSON.parse(barS0!.getAttribute("data-radius") || "null")).toEqual(0);
      // Top bar (last): radius [4,4,0,0]
      expect(JSON.parse(barS1!.getAttribute("data-radius") || "null")).toEqual([
        4, 4, 0, 0,
      ]);
    });

    it("fills each slice from the caller-supplied colour list", () => {
      const { container } = renderSliced();
      const barS0 = container.querySelector('[data-testid="bar-s0"]');
      const barS1 = container.querySelector('[data-testid="bar-s1"]');
      expect(barS0?.getAttribute("data-fill")).toBe("var(--color-chart-1)");
      expect(barS1?.getAttribute("data-fill")).toBe("var(--color-chart-2)");
      expect(barS0?.getAttribute("data-fill")).not.toBe(
        barS1?.getAttribute("data-fill"),
      );
    });

    it("honours a neutral 'Other' colour rather than recomputing from the index", () => {
      const { container } = renderSliced({
        secondaryValues: ["CatA", "Other"],
        sliceColors: [CHART_SERIES[0], "var(--color-border-strong)"],
      });
      expect(
        container
          .querySelector('[data-testid="bar-s1"]')
          ?.getAttribute("data-fill"),
      ).toBe("var(--color-border-strong)");
    });
  });

  // ── F5 ──────────────────────────────────────────────────────────────
  describe("F5: `stacked` flips the break-down between stacked and grouped", () => {
    it("stacked=true puts every slice on one stackId", () => {
      const { container } = renderSliced({ stacked: true });
      expect(
        container
          .querySelector('[data-testid="bar-s0"]')
          ?.getAttribute("data-stack-id"),
      ).toBe("stack");
      expect(
        container
          .querySelector('[data-testid="bar-s1"]')
          ?.getAttribute("data-stack-id"),
      ).toBe("stack");
    });

    it("stacked=false leaves stackId undefined so the slices sit side by side", () => {
      const { container } = renderSliced({ stacked: false });
      expect(
        container
          .querySelector('[data-testid="bar-s0"]')
          ?.getAttribute("data-stack-id"),
      ).toBe("undefined");
      expect(
        container
          .querySelector('[data-testid="bar-s1"]')
          ?.getAttribute("data-stack-id"),
      ).toBe("undefined");
      // Grouped: every bar is its own column, so every bar gets a cap.
      expect(
        JSON.parse(
          container
            .querySelector('[data-testid="bar-s0"]')!
            .getAttribute("data-radius") || "null",
        ),
      ).toEqual([4, 4, 0, 0]);
    });
  });

  // ── F27 ─────────────────────────────────────────────────────────────
  describe("F27: reduced motion", () => {
    it("disables the JS animation even for a viewer who asked for reduced motion", () => {
      // globals.css implements prefers-reduced-motion as a CSS block zeroing
      // animation-duration. Recharts animates through react-smooth's rAF loop
      // writing inline attributes per frame, so that block never reaches it.
      const original = window.matchMedia;
      window.matchMedia = ((query: string) => ({
        matches: query.includes("prefers-reduced-motion"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })) as unknown as typeof window.matchMedia;
      try {
        expect(window.matchMedia("(prefers-reduced-motion: reduce)").matches).toBe(
          true,
        );

        const { container } = renderSliced();
        for (const key of ["s0", "s1"]) {
          const bar = container.querySelector(`[data-testid="bar-${key}"]`);
          expect(bar?.getAttribute("data-is-animation-active")).toBe("false");
          expect(bar?.getAttribute("data-animation-duration")).toBe("undefined");
        }

        const single = render(
          <BarWidgetChart
            rows={rows}
            sliced={false}
            stacked
            secondaryValues={[]}
            seriesKeys={[]}
            sliceColors={[]}
            valueName="Amount"
            format="number"
          />,
        );
        expect(
          single.container
            .querySelector('[data-testid="bar-value"]')
            ?.getAttribute("data-is-animation-active"),
        ).toBe("false");
      } finally {
        window.matchMedia = original;
      }
    });
  });

  // ── F28 ─────────────────────────────────────────────────────────────
  describe("F28: WCAG 1.4.11 segment separator", () => {
    it("gives every Bar a surface-coloured stroke so adjacent segments are distinguishable", () => {
      // Measured across all 28 palette pairs, segment-vs-segment contrast is
      // 1.05–1.59. Surface-vs-chart-N is 3.13–9.48, so a 1px surface stroke
      // is what carries the 3:1 boundary.
      const { container } = renderSliced();
      for (const key of ["s0", "s1"]) {
        const bar = container.querySelector(`[data-testid="bar-${key}"]`);
        expect(bar?.getAttribute("data-stroke")).toBe("var(--color-surface)");
        expect(bar?.getAttribute("data-stroke-width")).toBe("1");
      }
    });
  });
});
