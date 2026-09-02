/**
 * TBD-383 — the Stacked bar entry in the widget picker must name the time
 * break-down.
 *
 * `dimensions: ["month", "category"]` is exactly how "a category over time" is
 * expressed today, and Stacked bar is the only widget that draws it. The
 * description said only "Bars split by a second dimension, stacked or side by
 * side" — no "time", no hint of a month axis — so a user looking for a
 * category-over-time chart had nothing to lead them here.
 *
 * ⚠ It stays in the "Categories" group deliberately. It is genuinely both, and
 * moving it would break the reader's grouping model without adding any
 * capability. Both halves are fenced: the copy AND the grouping.
 */
import { render, screen, within } from "@testing-library/react";

import WidgetPicker from "@/components/reports/WidgetPicker";

function open() {
  render(<WidgetPicker open onClose={() => {}} onPick={() => {}} />);
  return screen.getByTestId("widget-picker-option-stacked_bar");
}

describe("WidgetPicker — Stacked bar", () => {
  it("names the time break-down, not just 'a second dimension'", () => {
    const option = open();
    // Asserted as a property of the copy, not as a literal string, so a
    // rewording that still names the time axis does not go red.
    expect(option.textContent?.toLowerCase()).toMatch(/month|time|over time/);
  });

  it("still describes the second-dimension split and the stack/group choice", () => {
    const text = open().textContent?.toLowerCase() ?? "";
    expect(text).toContain("second dimension");
    expect(text).toMatch(/stacked/);
    expect(text).toMatch(/side by side/);
  });

  it("⚠ stays in the Categories group", () => {
    open();
    const categories = screen.getByTestId("widget-picker-group-Categories");
    expect(
      within(categories).getByTestId("widget-picker-option-stacked_bar"),
    ).toBeInTheDocument();
    // ...and is NOT in Trends, which is the move this fence exists to refuse.
    const trends = screen.getByTestId("widget-picker-group-Trends");
    expect(
      within(trends).queryByTestId("widget-picker-option-stacked_bar"),
    ).toBeNull();
  });
});
