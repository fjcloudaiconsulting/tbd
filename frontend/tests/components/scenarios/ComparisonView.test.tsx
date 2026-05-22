/**
 * Tests for ComparisonView (PR3 of the Plans train).
 *
 * Architect-locked checks:
 * - Renders the chart wrapper and the verdict matrix.
 * - One verdict row per scenario.
 * - sharedYDomain spans the max range across every scenario.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import {
  ComparisonView,
  sharedYDomain,
  type CompareProjection,
} from "@/components/scenarios/ComparisonView";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-responsive">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-line-chart">{children}</div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Line: () => null,
}));

function makeProjection(
  id: number,
  name: string,
  balances: number[],
  color: "green" | "yellow" | "red",
): CompareProjection {
  return {
    scenario_id: id,
    name,
    scenario_type: "trip",
    projection: {
      engine_name: "analytic_v1",
      horizon_months: balances.length,
      currency: "EUR",
      per_account_series: [
        {
          account_id: 12,
          account_name: "Main",
          currency: "EUR",
          points: balances.map((b, i) => ({
            month: `2026-${String(i + 1).padStart(2, "0")}`,
            projected_balance: b.toFixed(2),
          })),
        },
      ],
      alerts: [],
      verdict: { color, headline: "h", reason: "r" },
    },
  };
}

describe("ComparisonView", () => {
  it("renders one verdict-matrix row per scenario", () => {
    const projs = [
      makeProjection(1, "A", [1000, 2000], "green"),
      makeProjection(2, "B", [500, 1500], "yellow"),
      makeProjection(3, "C", [-100, 50], "red"),
    ];
    render(<ComparisonView projections={projs} />);
    expect(screen.getByTestId("comparison-view-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("comparison-view-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("comparison-view-row-3")).toBeInTheDocument();
    expect(screen.getByTestId("comparison-view-verdict-1")).toHaveTextContent(
      "GREEN",
    );
    expect(screen.getByTestId("comparison-view-verdict-2")).toHaveTextContent(
      "YELLOW",
    );
    expect(screen.getByTestId("comparison-view-verdict-3")).toHaveTextContent(
      "RED",
    );
  });

  it("renders the empty state when projections is empty", () => {
    render(<ComparisonView projections={[]} />);
    expect(
      screen.getByTestId("comparison-view-empty"),
    ).toBeInTheDocument();
  });

  it("renders the chart wrapper for non-empty projections", () => {
    const projs = [makeProjection(1, "A", [1000, 2000], "green")];
    render(<ComparisonView projections={projs} />);
    expect(screen.getByTestId("comparison-view-chart")).toBeInTheDocument();
  });

  it("sharedYDomain spans the global max across scenarios", () => {
    const projs = [
      makeProjection(1, "Small", [100, 200], "green"),
      makeProjection(2, "Big", [10000, 12000], "green"),
    ];
    const months = ["2026-01", "2026-02"];
    const [lo, hi] = sharedYDomain(projs, months);
    // Floor at 0 since nothing dips negative.
    expect(lo).toBe(0);
    // Upper bound includes the 12000 peak + some padding.
    expect(hi).toBeGreaterThanOrEqual(12000);
  });

  it("sharedYDomain reaches negative values when a scenario dips below zero", () => {
    const projs = [makeProjection(1, "Dip", [-500, 100], "red")];
    const months = ["2026-01", "2026-02"];
    const [lo] = sharedYDomain(projs, months);
    expect(lo).toBeLessThan(0);
  });

  it("renders alerts column when projections include alerts", () => {
    const proj = makeProjection(1, "Alert", [-100, 50], "red");
    proj.projection.alerts = [
      {
        account_id: 12,
        month: "2026-01",
        projected_balance: "-100.00",
        trigger: "trip_lump_sum",
        severity: "warn",
      },
    ];
    render(<ComparisonView projections={[proj]} />);
    expect(
      screen.getByTestId("comparison-view-alerts-1"),
    ).toHaveTextContent("1 dip");
  });

  it("invokes onOpen with the scenario id when Open is clicked", () => {
    const onOpen = vi.fn();
    const projs = [makeProjection(1, "Trip", [1000, 1100], "green")];
    render(<ComparisonView projections={projs} onOpen={onOpen} />);
    screen.getByTestId("comparison-view-open-1").click();
    expect(onOpen).toHaveBeenCalledWith(1);
  });
});
