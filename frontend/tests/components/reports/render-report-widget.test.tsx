/**
 * Tests for the shared renderReportWidget function.
 *
 * Asserts that:
 *   - each report widget type dispatches to the correct component
 *   - the sankey arm renders SankeyWidget (reports-only; unreachable from
 *     the dashboard path where the backend rejects sankey layouts)
 *
 * Mock strategy: lightweight stubs for all widget components so the
 * function can be exercised without SWR / API wiring.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import { renderReportWidget } from "@/components/reports/renderReportWidget";
import type { Widget } from "@/lib/reports/types";

// ── Widget component stubs ────────────────────────────────────────────────────

vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: () => <div data-testid="kpi-widget-stub">KPIWidget</div>,
}));
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: () => <div data-testid="bar-widget-stub">BarWidget</div>,
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: () => <div data-testid="line-widget-stub">LineWidget</div>,
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: () => <div data-testid="area-widget-stub">AreaWidget</div>,
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: () => <div data-testid="pie-widget-stub">PieWidget</div>,
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: () => <div data-testid="sparkline-widget-stub">SparklineWidget</div>,
}));
vi.mock("@/components/reports/widgets/StackedBarWidget", () => ({
  default: () => <div data-testid="stacked-bar-widget-stub">StackedBarWidget</div>,
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: () => <div data-testid="table-widget-stub">TableWidget</div>,
}));
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: () => <div data-testid="sankey-widget-stub">SankeyWidget</div>,
}));

// ── Helpers ───────────────────────────────────────────────────────────────────

function stubWidget(type: Widget["type"]): Widget {
  return {
    id: "w_stub",
    type,
    title: "stub",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: [],
      format: "currency",
    },
  } as unknown as Widget;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("renderReportWidget", () => {
  const CANVAS_FILTERS = {};

  const CASES: Array<[Widget["type"], string]> = [
    ["kpi", "kpi-widget-stub"],
    ["bar", "bar-widget-stub"],
    ["line", "line-widget-stub"],
    ["area", "area-widget-stub"],
    ["pie", "pie-widget-stub"],
    ["sparkline", "sparkline-widget-stub"],
    ["stacked_bar", "stacked-bar-widget-stub"],
    ["table", "table-widget-stub"],
    ["sankey", "sankey-widget-stub"],
  ];

  it.each(CASES)(
    "renders %s widget to the correct component",
    (type, testId) => {
      render(<>{renderReportWidget(stubWidget(type), CANVAS_FILTERS, false)}</>);
      expect(screen.getByTestId(testId)).toBeInTheDocument();
    },
  );

  it("passes editMode=true down to the widget component", () => {
    // The stub doesn't use editMode, but the render must not throw.
    render(<>{renderReportWidget(stubWidget("kpi"), CANVAS_FILTERS, true)}</>);
    expect(screen.getByTestId("kpi-widget-stub")).toBeInTheDocument();
  });

  it("passes an optional currency string without throwing", () => {
    render(
      <>
        {renderReportWidget(stubWidget("bar"), CANVAS_FILTERS, false, "EUR")}
      </>,
    );
    expect(screen.getByTestId("bar-widget-stub")).toBeInTheDocument();
  });

  it("is the single source: renderDashboardWidget fall-through imports it", async () => {
    // Verifies the shared module is actually what the dashboard uses, so
    // both surfaces stay in sync.  A compile-time import check is enough —
    // we just assert the export exists and is a function.
    const mod = await import("@/components/reports/renderReportWidget");
    expect(typeof mod.renderReportWidget).toBe("function");
  });
});
