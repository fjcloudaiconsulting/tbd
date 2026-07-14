/**
 * Phase 4b: the canvas filter bar is date-only for accounts/categories.
 * Feature 1 adds a shared Settled/Pending status control that cascades to
 * transactions widgets. The account/category pickers still must not
 * render here.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";
import type { CanvasFilters } from "@/lib/reports/types";

describe("CanvasFiltersBar", () => {
  it("renders the date and status controls (no account/category pickers)", () => {
    render(<CanvasFiltersBar value={{}} onChange={() => {}} />);
    expect(screen.getByTestId("canvas-filters-bar")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-chips")).toBeInTheDocument();
    expect(screen.getByTestId("status-filter")).toBeInTheDocument();
    expect(screen.queryByTestId("account-filter")).toBeNull();
    expect(screen.queryByTestId("category-picker")).toBeNull();
  });

  it("renders the reusable StatusFilter bound to value.status", () => {
    render(
      <CanvasFiltersBar value={{ status: "pending" }} onChange={() => {}} />,
    );
    // Canvas-prefixed aria labels, and the persisted value is reflected.
    expect(screen.getByLabelText("Canvas status All")).toBeInTheDocument();
    expect(screen.getByLabelText("Canvas status Settled")).toBeInTheDocument();
    const pending = screen.getByLabelText(
      "Canvas status Pending",
    ) as HTMLInputElement;
    expect(pending.checked).toBe(true);
  });

  it("merges the chosen status into value on change", () => {
    const calls: CanvasFilters[] = [];
    render(
      <CanvasFiltersBar
        value={{ date_range: { start: "2026-01-01" } }}
        onChange={(next) => calls.push(next)}
      />,
    );
    fireEvent.click(screen.getByLabelText("Canvas status Settled"));
    // The existing date_range is preserved; status is merged in.
    expect(calls.at(-1)).toEqual({
      date_range: { start: "2026-01-01" },
      status: "settled",
    });
  });

  it("hides the date block when hideDate is set (status only)", () => {
    render(<CanvasFiltersBar hideDate value={{}} onChange={() => {}} />);
    expect(screen.queryByTestId("date-preset-chips")).toBeNull();
    expect(screen.getByTestId("status-filter")).toBeInTheDocument();
  });
});
