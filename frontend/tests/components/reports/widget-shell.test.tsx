/**
 * WidgetShell — mounts the per-widget filter-chip header above the
 * widget body in BOTH view and edit mode, and wires chip clicks to
 * ``onSelectFilters`` (which the page uses to select the widget and
 * deep-link the popover to the Filters tab).
 */
import { fireEvent, render, screen } from "@testing-library/react";

import WidgetShell from "@/components/reports/WidgetShell";
import type { BarWidget, WidgetFilters } from "@/lib/reports/types";

function barWith(filters: WidgetFilters): BarWidget {
  return {
    id: "w1",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      filters,
    },
  };
}

function renderShell(editMode: boolean, onSelectFilters = () => {}) {
  return render(
    <WidgetShell
      widgetId="w1"
      selected={false}
      editMode={editMode}
      onSelect={() => {}}
      onSelectFilters={onSelectFilters}
      widget={barWith({ txn_type: "expense" })}
      canvasFilters={{}}
      accounts={[]}
      categories={[]}
    >
      <div>body</div>
    </WidgetShell>,
  );
}

describe("WidgetShell filter chips", () => {
  it("renders filter chips in view mode and fires onSelectFilters", () => {
    const onSelectFilters = vi.fn();
    renderShell(false, onSelectFilters);
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    expect(onSelectFilters).toHaveBeenCalledOnce();
  });

  it("renders filter chips in edit mode too", () => {
    renderShell(true);
    expect(screen.getByTestId("widget-filter-chip-txn_type")).toBeInTheDocument();
  });

  it("does not fire the shell onSelect when a chip is clicked", () => {
    const onSelect = vi.fn();
    render(
      <WidgetShell
        widgetId="w1"
        selected={false}
        editMode={false}
        onSelect={onSelect}
        onSelectFilters={() => {}}
        widget={barWith({ txn_type: "expense" })}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
      >
        <div>body</div>
      </WidgetShell>,
    );
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
