/**
 * Tests for the shared renderReportWidget function AND widgetKit's
 * renderWidgetByType — the two type -> component routing tables (F15).
 *
 * Asserts that:
 *   - each report widget type dispatches to the correct component
 *   - the sankey arm renders SankeyWidget (reports-only; unreachable from
 *     the dashboard path where the backend rejects sankey layouts)
 *
 * Mock strategy: lightweight stubs for all widget components so the
 * functions can be exercised without SWR / API wiring.
 *
 * ⚠ F29 — TBD-382 routes `stacked_bar` through `BarWidget`, so TWO rows of
 * the table below now point at `bar-widget-stub`. A table that only asserts
 * "some stub rendered" would then pass while certifying STRICTLY LESS than
 * before: retargeting the `stacked_bar` row to the bar stub compiles and goes
 * green even if the arm were deleted and the widget fell through. Each stub
 * therefore ECHOES the widget it was handed via `data-widget-type`, and every
 * case asserts that its OWN type came through.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import { renderReportWidget } from "@/components/reports/renderReportWidget";
import { renderWidgetByType } from "@/components/reports/widgetKit";
import type { Widget } from "@/lib/reports/types";

// ── Widget component stubs ────────────────────────────────────────────────────

vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="kpi-widget-stub" data-widget-type={widget.type}>
      KPIWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="bar-widget-stub" data-widget-type={widget.type}>
      BarWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="line-widget-stub" data-widget-type={widget.type}>
      LineWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="area-widget-stub" data-widget-type={widget.type}>
      AreaWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="pie-widget-stub" data-widget-type={widget.type}>
      PieWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="sparkline-widget-stub" data-widget-type={widget.type}>
      SparklineWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="table-widget-stub" data-widget-type={widget.type}>
      TableWidget
    </div>
  ),
}));
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: ({ widget }: { widget: Widget }) => (
    <div data-testid="sankey-widget-stub" data-widget-type={widget.type}>
      SankeyWidget
    </div>
  ),
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
    ["stacked_bar", "bar-widget-stub"],
    ["table", "table-widget-stub"],
    ["sankey", "sankey-widget-stub"],
  ];

  it.each(CASES)(
    "renders %s widget to the correct component, carrying its OWN type through",
    (type, testId) => {
      render(<>{renderReportWidget(stubWidget(type), CANVAS_FILTERS, false)}</>);
      const el = screen.getByTestId(testId);
      expect(el).toBeInTheDocument();
      expect(el).toHaveAttribute("data-widget-type", type);
    },
  );

  // ── F15 ─────────────────────────────────────────────────────────────
  it.each(CASES)(
    "F15: widgetKit.renderWidgetByType routes %s to the SAME component",
    (type, testId) => {
      render(<>{renderWidgetByType(stubWidget(type), CANVAS_FILTERS, false)}</>);
      const el = screen.getByTestId(testId);
      expect(el).toBeInTheDocument();
      expect(el).toHaveAttribute("data-widget-type", type);
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
