import { fireEvent, render, screen } from "@testing-library/react";

import AmountRangeFilter from "@/components/reports/filters/AmountRangeFilter";

type Range = { min?: number; max?: number } | undefined;

describe("AmountRangeFilter", () => {
  it("renders the min and max inputs", () => {
    render(<AmountRangeFilter value={undefined} onChange={() => {}} />);
    expect(screen.getByLabelText("Amount min")).toBeInTheDocument();
    expect(screen.getByLabelText("Amount max")).toBeInTheDocument();
  });

  it("shows the current bounds", () => {
    render(
      <AmountRangeFilter value={{ min: 5, max: 20 }} onChange={() => {}} />,
    );
    expect(screen.getByLabelText("Amount min")).toHaveValue(5);
    expect(screen.getByLabelText("Amount max")).toHaveValue(20);
  });

  it("shows empty inputs when no bounds are set", () => {
    render(<AmountRangeFilter value={undefined} onChange={() => {}} />);
    expect(screen.getByLabelText("Amount min")).toHaveValue(null);
    expect(screen.getByLabelText("Amount max")).toHaveValue(null);
  });

  it("reports the min bound on change", () => {
    const calls: Range[] = [];
    render(
      <AmountRangeFilter value={undefined} onChange={(v) => calls.push(v)} />,
    );
    fireEvent.change(screen.getByLabelText("Amount min"), {
      target: { value: "5" },
    });
    expect(calls.at(-1)).toEqual({ min: 5 });
  });

  it("reports the max bound while preserving an existing min", () => {
    const calls: Range[] = [];
    render(
      <AmountRangeFilter value={{ min: 5 }} onChange={(v) => calls.push(v)} />,
    );
    fireEvent.change(screen.getByLabelText("Amount max"), {
      target: { value: "20" },
    });
    expect(calls.at(-1)).toEqual({ min: 5, max: 20 });
  });

  it("accepts decimal amounts", () => {
    const calls: Range[] = [];
    render(
      <AmountRangeFilter value={undefined} onChange={(v) => calls.push(v)} />,
    );
    fireEvent.change(screen.getByLabelText("Amount min"), {
      target: { value: "12.5" },
    });
    expect(calls.at(-1)).toEqual({ min: 12.5 });
  });

  it("clears a bound to undefined when emptied", () => {
    const calls: Range[] = [];
    render(
      <AmountRangeFilter value={{ min: 5 }} onChange={(v) => calls.push(v)} />,
    );
    fireEvent.change(screen.getByLabelText("Amount min"), {
      target: { value: "" },
    });
    // Only bound cleared → whole range collapses to undefined.
    expect(calls.at(-1)).toBeUndefined();
  });

  it("keeps the other bound when one is cleared", () => {
    const calls: Range[] = [];
    render(
      <AmountRangeFilter
        value={{ min: 5, max: 20 }}
        onChange={(v) => calls.push(v)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Amount min"), {
      target: { value: "" },
    });
    expect(calls.at(-1)).toEqual({ max: 20 });
  });

  it("honors the ariaPrefix prop", () => {
    render(
      <AmountRangeFilter
        value={undefined}
        ariaPrefix="Widget amount"
        onChange={() => {}}
      />,
    );
    expect(screen.getByLabelText("Widget amount min")).toBeInTheDocument();
    expect(screen.getByLabelText("Widget amount max")).toBeInTheDocument();
  });
});
