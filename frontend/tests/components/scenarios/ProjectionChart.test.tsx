/**
 * ProjectionChart regression test for the Recharts width(-1) / height(-1)
 * warning that fired in production when the right pane of the Plans
 * editor first mounted.
 *
 * Root cause:
 *   `ResponsiveContainer` measures its parent on synchronous first
 *   render. In a CSS-grid pane with `minmax(0, 2fr)`, that read can
 *   come back as -1 before the layout engine has committed and
 *   Recharts logs the loud `width(-1) and height(-1) of chart should
 *   be greater than 0` warning.
 *
 * Guardrail:
 *   The fix defers the ResponsiveContainer render by one effect tick
 *   (a `mounted` flag), and pins `min-w-0 min-h-0` on the wrapper so
 *   the grid track can shrink to fit. This test spies on
 *   `console.warn` and asserts the bad string never reaches it after
 *   the chart renders with valid fixture data.
 */
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";

import { ProjectionChart } from "@/components/scenarios/ProjectionChart";

// Stub Recharts the same way the page test does. The structural stub
// keeps it out of the way of jsdom's SVG pipeline while still letting
// us assert that the chart wrapper renders.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-responsive">{children}</div>
  ),
  ComposedChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-chart">{children}</div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Area: () => null,
  Line: () => null,
  ReferenceDot: () => null,
  Dot: () => null,
}));

const FIXTURE_PROJECTION = {
  currency: "EUR",
  per_account_series: [
    {
      account_id: 1,
      account_name: "Main",
      currency: "EUR",
      points: [
        { month: "2026-06", projected_balance: "1000.00" },
        { month: "2026-07", projected_balance: "1100.00" },
        { month: "2026-08", projected_balance: "1200.00" },
      ],
    },
  ],
  alerts: [],
  real_terms_series: null,
};

describe("ProjectionChart", () => {
  it("does not log the Recharts width(-1)/height(-1) warning on mount", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      render(<ProjectionChart projection={FIXTURE_PROJECTION} />);
      // Wait through React's effect tick so the deferred
      // ResponsiveContainer paint actually runs.
      await waitFor(() => {
        expect(screen.getByTestId("projection-chart")).toBeInTheDocument();
      });
      // Give effects another microtask to settle before we inspect
      // the warn log; the symptom in prod was warnings firing right
      // around the first paint.
      await act(async () => {});
      const badCalls = warnSpy.mock.calls.filter((call) =>
        call.some(
          (arg) =>
            typeof arg === "string"
            && /width.*height.*should be greater/i.test(arg),
        ),
      );
      expect(badCalls).toEqual([]);
    } finally {
      warnSpy.mockRestore();
    }
  });

  it("renders the empty placeholder when no months are projected", () => {
    render(
      <ProjectionChart
        projection={{ ...FIXTURE_PROJECTION, per_account_series: [] }}
      />,
    );
    expect(screen.getByTestId("projection-chart-empty")).toBeInTheDocument();
  });

  it("pins min-w-0 min-h-0 on the chart wrapper so the grid track can shrink", async () => {
    render(<ProjectionChart projection={FIXTURE_PROJECTION} />);
    const wrapper = await screen.findByTestId("projection-chart");
    expect(wrapper.className).toMatch(/min-w-0/);
    expect(wrapper.className).toMatch(/min-h-0/);
  });
});
