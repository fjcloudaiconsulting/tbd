/**
 * Phase 4b: the canvas filter bar is now date-only. Accounts and
 * categories moved to per-widget editing, so the account/category
 * pickers must no longer render here — only the shared date control.
 */
import { render, screen } from "@testing-library/react";

import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";

describe("CanvasFiltersBar", () => {
  it("renders only the date control (no account/category pickers)", () => {
    render(<CanvasFiltersBar value={{}} onChange={() => {}} />);
    expect(screen.getByTestId("canvas-filters-bar")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-chips")).toBeInTheDocument();
    expect(screen.queryByTestId("account-filter")).toBeNull();
    expect(screen.queryByTestId("category-picker")).toBeNull();
  });
});
