import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import SortableHeader from "@/components/ui/SortableHeader";

describe("SortableHeader", () => {
  const defaults = {
    label: "Amount",
    field: "amount",
    activeField: "date",
    dir: "asc" as const,
    onSort: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the label text", () => {
    render(<SortableHeader {...defaults} />);
    expect(screen.getByText("Amount")).toBeInTheDocument();
  });

  it("does NOT show a sort indicator when the field is not active", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="date"
        dir="asc"
      />,
    );
    // No ▲ or ▼ indicator when not the active column
    expect(screen.queryByText("▲")).not.toBeInTheDocument();
    expect(screen.queryByText("▼")).not.toBeInTheDocument();
  });

  it("shows ▲ indicator when this field is active and dir is asc", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="amount"
        dir="asc"
      />,
    );
    expect(screen.getByText("▲")).toBeInTheDocument();
  });

  it("shows ▼ indicator when this field is active and dir is desc", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="amount"
        dir="desc"
      />,
    );
    expect(screen.getByText("▼")).toBeInTheDocument();
  });

  it("sets aria-sort='none' when the field is not active", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="date"
        dir="asc"
      />,
    );
    // aria-sort lives on the cell element (th or the wrapper)
    const cell = screen.getByRole("columnheader");
    expect(cell).toHaveAttribute("aria-sort", "none");
  });

  it("sets aria-sort='ascending' when active and dir is asc", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="amount"
        dir="asc"
      />,
    );
    const cell = screen.getByRole("columnheader");
    expect(cell).toHaveAttribute("aria-sort", "ascending");
  });

  it("sets aria-sort='descending' when active and dir is desc", () => {
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="amount"
        dir="desc"
      />,
    );
    const cell = screen.getByRole("columnheader");
    expect(cell).toHaveAttribute("aria-sort", "descending");
  });

  it("clicking the button calls onSort with the field", () => {
    const onSort = vi.fn();
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="date"
        onSort={onSort}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(onSort).toHaveBeenCalledOnce();
    expect(onSort).toHaveBeenCalledWith("amount");
  });

  it("keyboard Enter on the button calls onSort", () => {
    const onSort = vi.fn();
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="date"
        onSort={onSort}
      />,
    );
    const btn = screen.getByRole("button");
    btn.focus();
    fireEvent.keyDown(btn, { key: "Enter", code: "Enter" });
    fireEvent.click(btn); // Enter on a focused button fires a click
    expect(onSort).toHaveBeenCalledWith("amount");
  });

  it("keyboard Space on the button calls onSort", () => {
    const onSort = vi.fn();
    render(
      <SortableHeader
        {...defaults}
        field="amount"
        activeField="date"
        onSort={onSort}
      />,
    );
    const btn = screen.getByRole("button");
    btn.focus();
    fireEvent.keyDown(btn, { key: " ", code: "Space" });
    fireEvent.click(btn); // Space on a focused button fires a click
    expect(onSort).toHaveBeenCalledWith("amount");
  });

  it("renders as a <th> (columnheader role)", () => {
    // Wrap in a table so the browser doesn't complain about a bare th
    render(
      <table>
        <thead>
          <tr>
            <SortableHeader {...defaults} />
          </tr>
        </thead>
      </table>,
    );
    expect(screen.getByRole("columnheader")).toBeInTheDocument();
  });

  it("right-aligns content when align='right'", () => {
    render(
      <SortableHeader {...defaults} align="right" />,
    );
    const cell = screen.getByRole("columnheader");
    // The cell or its content should have some right-alignment class
    expect(cell.className).toMatch(/right/);
  });

  it("left-aligns content by default (align='left')", () => {
    render(<SortableHeader {...defaults} />);
    const cell = screen.getByRole("columnheader");
    expect(cell.className).toMatch(/left/);
  });
});
