/**
 * WidgetFilterChips — the per-widget filter-chip header row. Renders a
 * pill button per effective non-default filter; clicking one selects the
 * widget and opens the popover's Filters tab via ``onSelectFilters``.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import WidgetFilterChips from "@/components/reports/WidgetFilterChips";
import type { BarWidget, WidgetFilters } from "@/lib/reports/types";

function barWith(filters: WidgetFilters): BarWidget {
  return {
    id: "w_bar",
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

describe("WidgetFilterChips", () => {
  it("renders a chip per set filter and fires onSelectFilters on click", () => {
    const onSelectFilters = vi.fn();
    render(
      <WidgetFilterChips
        widget={barWith({ txn_type: "expense" })}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        onSelectFilters={onSelectFilters}
      />,
    );
    fireEvent.click(screen.getByTestId("widget-filter-chip-txn_type"));
    expect(onSelectFilters).toHaveBeenCalledOnce();
  });

  it("renders nothing when the widget has no set filters", () => {
    const { container } = render(
      <WidgetFilterChips
        widget={barWith({})}
        canvasFilters={{}}
        accounts={[]}
        categories={[]}
        onSelectFilters={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
