import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * Tests covering the "sankey" widget type wiring:
 *  - emptyWidget("sankey", id) returns a valid SankeyWidget with sane defaults
 *  - WidgetPicker lists a "Cash flow (Sankey)" option
 *  - renderWidgetByType returns <SankeyWidget> for a sankey widget
 */

// Mock SankeyWidget so renderWidgetByType tests don't need a live SWR/Nivo tree
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: ({ widget }: { widget: { id: string } }) => (
    <div data-testid="sankey-widget-mock" data-widget-id={widget.id} />
  ),
}));

import { emptyWidget, renderWidgetByType } from "@/components/reports/widgetKit";
import WidgetPicker from "@/components/reports/WidgetPicker";
import type { SankeyWidget } from "@/lib/reports/types";

const CANVAS_FILTERS = {};

describe("emptyWidget('sankey')", () => {
  it("returns a widget with type 'sankey'", () => {
    const w = emptyWidget("sankey", "w_test_1");
    expect(w.type).toBe("sankey");
  });

  it("returns a widget with the provided id", () => {
    const w = emptyWidget("sankey", "w_abc");
    expect(w.id).toBe("w_abc");
  });

  it("has dataset='transactions'", () => {
    const w = emptyWidget("sankey", "w_test_2") as SankeyWidget;
    expect(w.config.dataset).toBe("transactions");
  });

  it("has measure sum/amount", () => {
    const w = emptyWidget("sankey", "w_test_3") as SankeyWidget;
    expect(w.config.measure).toEqual({ agg: "sum", field: "amount" });
  });

  it("defaults spending_granularity to 'category'", () => {
    const w = emptyWidget("sankey", "w_test_4") as SankeyWidget;
    expect(w.config.spending_granularity).toBe("category");
  });

  it("has a reasonable grid size (wide)", () => {
    const w = emptyWidget("sankey", "w_test_5");
    // Sankey needs horizontal room — width should be at least 6
    expect(w.grid.w).toBeGreaterThanOrEqual(6);
  });
});

describe("WidgetPicker — sankey entry", () => {
  it("renders a 'Cash flow (Sankey)' option", () => {
    render(
      <WidgetPicker open={true} onClose={() => {}} onPick={() => {}} />,
    );
    expect(
      screen.getByTestId("widget-picker-option-sankey"),
    ).toBeInTheDocument();
    expect(screen.getByText("Cash flow (Sankey)")).toBeInTheDocument();
  });

  it("calls onPick with 'sankey' when the option is clicked", async () => {
    const onPick = vi.fn();
    const { getByTestId } = render(
      <WidgetPicker open={true} onClose={() => {}} onPick={onPick} />,
    );
    getByTestId("widget-picker-option-sankey").click();
    expect(onPick).toHaveBeenCalledWith("sankey");
  });
});

describe("renderWidgetByType — sankey", () => {
  it("returns a SankeyWidget for a sankey widget", () => {
    const w = emptyWidget("sankey", "w_render_test") as SankeyWidget;
    const { getByTestId } = render(
      <>{renderWidgetByType(w, CANVAS_FILTERS, false)}</>,
    );
    expect(getByTestId("sankey-widget-mock")).toBeInTheDocument();
    expect(getByTestId("sankey-widget-mock").getAttribute("data-widget-id")).toBe(
      "w_render_test",
    );
  });
});
