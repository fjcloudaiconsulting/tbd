import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import Pagination from "@/components/ui/Pagination";
import { PAGE_SIZE_OPTIONS } from "@/lib/hooks/use-table-state";

describe("Pagination", () => {
  const defaults = {
    page: 1,
    pageSize: 25,
    total: 100,
    onPageChange: vi.fn(),
    onPageSizeChange: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the status line with correct values", () => {
    render(<Pagination {...defaults} />);
    // "Page 1 of 4 · 100 total" (4 pages = ceil(100/25))
    expect(screen.getByText(/Page 1 of 4/)).toBeInTheDocument();
    expect(screen.getByText(/100 total/)).toBeInTheDocument();
  });

  it("does NOT contain an em-dash anywhere in rendered text", () => {
    const { container } = render(<Pagination {...defaults} />);
    expect(container.textContent).not.toContain("—"); // em-dash
  });

  it("uses a middot separator not an em-dash", () => {
    render(<Pagination {...defaults} />);
    // The middot (·) should be present
    expect(screen.getByText(/·/)).toBeInTheDocument();
  });

  it("Previous button is disabled on page 1", () => {
    render(<Pagination {...defaults} page={1} />);
    const prev = screen.getByRole("button", { name: /previous page/i });
    expect(prev).toBeDisabled();
  });

  it("Next button is enabled on page 1 when there are more pages", () => {
    render(<Pagination {...defaults} page={1} total={100} pageSize={25} />);
    const next = screen.getByRole("button", { name: /next page/i });
    expect(next).not.toBeDisabled();
  });

  it("Next button is disabled on the last page", () => {
    // 4 pages total, currently on page 4
    render(<Pagination {...defaults} page={4} total={100} pageSize={25} />);
    const next = screen.getByRole("button", { name: /next page/i });
    expect(next).toBeDisabled();
  });

  it("Previous button is enabled when not on page 1", () => {
    render(<Pagination {...defaults} page={2} />);
    const prev = screen.getByRole("button", { name: /previous page/i });
    expect(prev).not.toBeDisabled();
  });

  it("clicking Next fires onPageChange with page + 1", () => {
    const onPageChange = vi.fn();
    render(
      <Pagination {...defaults} page={2} onPageChange={onPageChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    expect(onPageChange).toHaveBeenCalledOnce();
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it("clicking Previous fires onPageChange with page - 1", () => {
    const onPageChange = vi.fn();
    render(
      <Pagination {...defaults} page={3} onPageChange={onPageChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /previous page/i }));
    expect(onPageChange).toHaveBeenCalledOnce();
    expect(onPageChange).toHaveBeenCalledWith(2);
  });

  it("changing the per-page select fires onPageSizeChange with the numeric value", () => {
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        {...defaults}
        onPageSizeChange={onPageSizeChange}
      />,
    );
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "50" } });
    expect(onPageSizeChange).toHaveBeenCalledOnce();
    expect(onPageSizeChange).toHaveBeenCalledWith(50);
  });

  it("renders the default PAGE_SIZE_OPTIONS in the select", () => {
    render(<Pagination {...defaults} />);
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) =>
      Number(o.value),
    );
    expect(optionValues).toEqual([...PAGE_SIZE_OPTIONS]);
  });

  it("accepts custom pageSizeOptions", () => {
    render(
      <Pagination {...defaults} pageSizeOptions={[5, 10, 20]} />,
    );
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) =>
      Number(o.value),
    );
    expect(optionValues).toEqual([5, 10, 20]);
  });

  it("the per-page select has an accessible label", () => {
    render(<Pagination {...defaults} />);
    // The label "Per page" must be associated with the select
    expect(screen.getByLabelText(/per page/i)).toBeInTheDocument();
  });

  it("shows page 1 of 1 when total is 0", () => {
    render(<Pagination {...defaults} total={0} />);
    expect(screen.getByText(/Page 1 of 1/)).toBeInTheDocument();
    expect(screen.getByText(/0 total/)).toBeInTheDocument();
  });

  it("both buttons are disabled when there is only 1 page", () => {
    render(<Pagination {...defaults} total={10} pageSize={25} page={1} />);
    expect(screen.getByRole("button", { name: /previous page/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /next page/i })).toBeDisabled();
  });

  it("shows the correct page count for a partial last page", () => {
    // 26 items / 25 per page = 2 pages
    render(<Pagination {...defaults} total={26} pageSize={25} page={1} />);
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
  });

  describe("showPageSizeSelector={false}", () => {
    it("does not render the per-page select", () => {
      render(<Pagination {...defaults} showPageSizeSelector={false} />);
      expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
      expect(screen.queryByLabelText(/per page/i)).not.toBeInTheDocument();
    });

    it("still renders the status line and navigation buttons", () => {
      render(
        <Pagination
          {...defaults}
          page={1}
          total={100}
          pageSize={25}
          showPageSizeSelector={false}
        />,
      );
      expect(screen.getByText(/Page 1 of 4/)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /next page/i }),
      ).not.toBeDisabled();
      expect(
        screen.getByRole("button", { name: /previous page/i }),
      ).toBeDisabled();
    });
  });

  describe("unique ids when multiple instances are rendered", () => {
    it("two Pagination instances have distinct select ids", () => {
      const { container } = render(
        <>
          <Pagination {...defaults} />
          <Pagination {...defaults} />
        </>,
      );
      const selects = container.querySelectorAll("select");
      expect(selects.length).toBe(2);
      const id0 = selects[0].id;
      const id1 = selects[1].id;
      expect(id0).toBeTruthy();
      expect(id1).toBeTruthy();
      expect(id0).not.toBe(id1);
    });

    it("each label is correctly associated with its own select (getByLabelText works with two instances)", () => {
      const { getAllByLabelText } = render(
        <>
          <Pagination {...defaults} />
          <Pagination {...defaults} />
        </>,
      );
      // getByLabelText would throw if the ids collide; getAllByLabelText returns both
      const perPageSelects = getAllByLabelText(/per page/i);
      expect(perPageSelects.length).toBe(2);
    });
  });
});
