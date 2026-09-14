import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { LineChart, Line } from "recharts";

import LineWidgetChart from "@/components/reports/widgets/LineWidgetChart";
import SparklineWidgetChart from "@/components/reports/widgets/SparklineWidgetChart";
import { ProjectionChart, type ProjectionInput } from "@/components/scenarios/ProjectionChart";

/**
 * TBD-437 / TBD-528: the app's recharts `<Line>` strokes are always fully
 * drawn, including under reduced motion, after a resize, and on new data.
 *
 * ## The defect this exists for (measured on real recharts 3.8.1)
 *
 * `Line.js` decides whether to override `stroke-dasharray` on the RAW
 * `isAnimationActive` prop. `"auto"` is a truthy string, so under reduced
 * motion (animation resolved OFF) it still builds a `"<len>px <total>px"`
 * dash, from `getTotalLength()` of the PREVIOUS commit's path, and nothing
 * re-renders to correct it. Measured in review: a `"4 2"` dashed line mounts
 * as `"0px, 0px"` (solid), a resize 300 -> 600 leaves a 326.9px dash on a
 * 609.2px path (46% of the line not drawn), and new data clips the tail.
 *
 * So the four app `<Line>` sites stay `isAnimationActive={false}` until the
 * recharts bump (TBD-528), and this fence renders the real components to prove
 * the strokes are whole.
 *
 * ## Why `getTotalLength` is computed from `d`, not a constant
 *
 * jsdom does not implement it. A CONSTANT stub makes every path the same
 * length, so a dash built from a stale path still "covers" the new one and a
 * resize or data change cannot expose the partial stroke. Measuring the
 * polyline through the numbers in `d` makes the length change whenever the
 * geometry does, which is the property the defect breaks.
 *
 * ⚠⚠ THIS FILE MUST NOT replace recharts' marks with stubs. Only
 * `ResponsiveContainer` is swapped, for a fixed size that the test can change.
 */

const size = vi.hoisted(() => ({ width: 300 }));

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  const R = await import("react");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) =>
      R.createElement(
        "div",
        null,
        R.cloneElement(children, { width: size.width, height: 200 } as never),
      ),
  };
});

/** Polyline length through every coordinate pair in the path's `d`. */
function lengthFromD(this: Element): number {
  const nums = ((this.getAttribute("d") ?? "").match(/-?\d*\.?\d+(?:e-?\d+)?/gi) ?? []).map(
    Number,
  );
  let total = 0;
  for (let i = 2; i + 1 < nums.length; i += 2) {
    total += Math.hypot(nums[i] - nums[i - 2], nums[i + 1] - nums[i - 1]);
  }
  return total;
}

function mockMatchMedia(reduce: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce && query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

let originalLength: PropertyDescriptor | undefined;

beforeEach(() => {
  size.width = 300;
  // Same harness as reduced-motion.test.tsx (see its TBD-459 note): faked
  // timers keep recharts' autobatch fallback timers off the real loop, and a
  // no-op rAF keeps any animation at its first frame.
  vi.useFakeTimers();
  vi.stubGlobal("requestAnimationFrame", () => 0);
  vi.stubGlobal("cancelAnimationFrame", () => {});
  originalLength = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getTotalLength");
  Object.defineProperty(SVGElement.prototype, "getTotalLength", {
    configurable: true,
    value: lengthFromD,
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  if (originalLength) Object.defineProperty(SVGElement.prototype, "getTotalLength", originalLength);
  else delete (SVGElement.prototype as unknown as Record<string, unknown>).getTotalLength;
});

type Curve = { stroke: string | null; dash: string | null; d: string | null };

function curves(container: HTMLElement): Curve[] {
  return Array.from(container.querySelectorAll(".recharts-line-curve")).map((p) => ({
    stroke: p.getAttribute("stroke"),
    dash: p.getAttribute("stroke-dasharray"),
    d: p.getAttribute("d"),
  }));
}

/** `dashedStroke` names the one line allowed a dash, and the exact dash it
 *  must carry. Every other curve must carry none. */
function expectWholeStrokes(
  label: string,
  got: Curve[],
  expectedCount: number,
  dashed?: { stroke: string; dash: string },
) {
  expect(got.length, `${label}: rendered curves`).toBe(expectedCount);
  for (const c of got) {
    expect(c.d, `${label}: curve has geometry`).toMatch(/\d/);
    expect(c.dash ?? "", `${label}: a px dash means a partially drawn stroke`).not.toMatch(/px/);
    if (dashed && c.stroke === dashed.stroke) {
      expect(c.dash, `${label}: the dashed line keeps its own pattern`).toBe(dashed.dash);
    } else {
      expect(c.dash, `${label}: an undashed line carries no dash`).toBeNull();
    }
  }
  if (dashed) {
    expect(
      got.filter((c) => c.stroke === dashed.stroke),
      `${label}: the dashed line rendered`,
    ).toHaveLength(1);
  }
}

/** Resize, then change the data, checking the strokes after each step.
 *  Also proves each step actually moved the geometry, so a harness that
 *  ignores the new width or data cannot pass by rendering the same path. */
function exercise(
  renderEl: (variant: 0 | 1) => React.ReactElement,
  expectedCount: number,
  dashed?: { stroke: string; dash: string },
) {
  const { container, rerender } = render(renderEl(0));
  const mount = curves(container);
  expectWholeStrokes("mount @300", mount, expectedCount, dashed);

  size.width = 600;
  rerender(renderEl(0));
  const resized = curves(container);
  expectWholeStrokes("resize @600", resized, expectedCount, dashed);
  expect(resized.map((c) => c.d)).not.toEqual(mount.map((c) => c.d));

  rerender(renderEl(1));
  const redrawn = curves(container);
  expectWholeStrokes("new data @600", redrawn, expectedCount, dashed);
  expect(redrawn.map((c) => c.d)).not.toEqual(resized.map((c) => c.d));
}

const LINE_ROWS = [
  [
    { label: "Jan", s0: 10, s1: 40 },
    { label: "Feb", s0: 40, s1: 20 },
    { label: "Mar", s0: 20, s1: 35 },
  ],
  [
    { label: "Jan", s0: 10, s1: 90 },
    { label: "Feb", s0: 90, s1: 0 },
    { label: "Mar", s0: 0, s1: 95 },
    { label: "Apr", s0: 60, s1: 5 },
  ],
];

function projection(variant: 0 | 1): ProjectionInput {
  const balances = variant === 0 ? ["1000", "1400", "1200"] : ["1000", "300", "2600", "900"];
  const months = ["2026-06", "2026-07", "2026-08", "2026-09"].slice(0, balances.length);
  const points = months.map((month, i) => ({ month, projected_balance: balances[i] }));
  return {
    currency: "EUR",
    per_account_series: [{ account_id: 1, account_name: "Main", currency: "EUR", points }],
    alerts: [],
    real_terms_series: {
      inflation_pct: "2",
      points: points.map((p) => ({ ...p, projected_balance: String(Number(p.projected_balance) * 0.9) })),
    },
  };
}

describe("TBD-528: app <Line> strokes are fully drawn under reduced motion", () => {
  it("LineWidgetChart: two series stay undashed across mount, resize and new data", () => {
    mockMatchMedia(true);
    exercise(
      (v) => (
        <LineWidgetChart
          rows={LINE_ROWS[v]}
          seriesKeys={["s0", "s1"]}
          labels={["A", "B"]}
          seriesColors={["var(--color-chart-1)", "var(--color-chart-2)"]}
          format="number"
        />
      ),
      2,
    );
  });

  it("SparklineWidgetChart: the trend line stays undashed across mount, resize and new data", () => {
    mockMatchMedia(true);
    exercise(
      (v) => (
        <SparklineWidgetChart
          rows={LINE_ROWS[v].map((r) => ({ label: r.label, value: r.s0 }))}
          format="number"
        />
      ),
      1,
    );
  });

  it('ProjectionChart: the real-terms line keeps exactly "4 2" across mount, resize and new data', () => {
    mockMatchMedia(true);
    exercise((v) => <ProjectionChart projection={projection(v)} />, 1, {
      stroke: "var(--color-danger)",
      dash: "4 2",
    });
  });

  it("positive control: the harness sees recharts' animated px dash on a Line that animates", () => {
    // No preference, the 220ms shape, no hard-off: recharts starts the
    // draw-on animation, whose first frame is `0px <total>px`. If this goes
    // red, the fence above can no longer see a dash at all and is vacuous.
    mockMatchMedia(false);
    const { container } = render(
      <LineChart width={300} height={100} data={LINE_ROWS[0]}>
        <Line dataKey="s0" dot={false} animationDuration={220} />
      </LineChart>,
    );
    const [c] = curves(container);
    expect(c?.dash ?? "").toMatch(/^0px [\d.]*[1-9][\d.]*px$/);
  });
});
