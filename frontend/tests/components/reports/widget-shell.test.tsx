/**
 * WidgetShell — mounts the per-widget filter-chip header above the
 * widget body in BOTH view and edit mode. The chips are INTERACTIVE only
 * in edit mode: ``WidgetShell`` passes ``interactive={editMode}`` to
 * ``WidgetFilterChips``. In edit mode a chip is a button wired to
 * ``onSelectFilters`` (select the widget + deep-link the popover's
 * Filters tab); in view mode the chips render as inert informational
 * spans (no false "edit" affordance for keyboard users).
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
      widget={barWith({ txn_type: ["expense"] })}
      canvasFilters={{}}
      accounts={[]}
      categories={[]}
    >
      <div>body</div>
    </WidgetShell>,
  );
}

describe("WidgetShell filter chips", () => {
  it("renders chips as inert spans in view mode (no onSelectFilters)", () => {
    const onSelectFilters = vi.fn();
    renderShell(false, onSelectFilters);
    const chip = screen.getByTestId("widget-filter-chip-txn_type");
    expect(chip.tagName).toBe("SPAN");
    fireEvent.click(chip);
    expect(onSelectFilters).not.toHaveBeenCalled();
  });

  it("renders interactive chip buttons in edit mode and fires onSelectFilters", () => {
    const onSelectFilters = vi.fn();
    renderShell(true, onSelectFilters);
    const chip = screen.getByTestId("widget-filter-chip-txn_type");
    expect(chip.tagName).toBe("BUTTON");
    fireEvent.click(chip);
    expect(onSelectFilters).toHaveBeenCalledOnce();
  });

  it("does not fire the shell onSelect when an edit-mode chip button is clicked", () => {
    // The chip button stops propagation so its select-with-Filters action
    // wins over the shell's plain onSelect.
    const onSelect = vi.fn();
    render(
      <WidgetShell
        widgetId="w1"
        selected={false}
        editMode
        onSelect={onSelect}
        onSelectFilters={() => {}}
        widget={barWith({ txn_type: ["expense"] })}
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
