import { fireEvent, render, screen } from "@testing-library/react";

import StatusFilter from "@/components/reports/filters/StatusFilter";
import type { TxnStatus } from "@/lib/reports/types";

describe("StatusFilter", () => {
  it("renders the three options (All / Settled / Pending)", () => {
    render(<StatusFilter value={undefined} onChange={() => {}} />);
    expect(screen.getByLabelText("Status All")).toBeInTheDocument();
    expect(screen.getByLabelText("Status Settled")).toBeInTheDocument();
    expect(screen.getByLabelText("Status Pending")).toBeInTheDocument();
  });

  it("checks 'All' when value is undefined", () => {
    render(<StatusFilter value={undefined} onChange={() => {}} />);
    expect(screen.getByLabelText("Status All")).toBeChecked();
    expect(screen.getByLabelText("Status Settled")).not.toBeChecked();
    expect(screen.getByLabelText("Status Pending")).not.toBeChecked();
  });

  it("checks the option matching the current value", () => {
    render(<StatusFilter value="pending" onChange={() => {}} />);
    expect(screen.getByLabelText("Status Pending")).toBeChecked();
    expect(screen.getByLabelText("Status All")).not.toBeChecked();
  });

  it("reports the selected status on change", () => {
    const calls: Array<TxnStatus | undefined> = [];
    render(<StatusFilter value={undefined} onChange={(v) => calls.push(v)} />);
    fireEvent.click(screen.getByLabelText("Status Settled"));
    expect(calls.at(-1)).toBe("settled");
  });

  it("reports undefined when 'All' is chosen", () => {
    const calls: Array<TxnStatus | undefined> = [];
    render(<StatusFilter value="settled" onChange={(v) => calls.push(v)} />);
    fireEvent.click(screen.getByLabelText("Status All"));
    expect(calls.at(-1)).toBeUndefined();
  });

  it("honors the ariaPrefix prop", () => {
    render(
      <StatusFilter
        value={undefined}
        ariaPrefix="Widget status"
        onChange={() => {}}
      />,
    );
    expect(screen.getByLabelText("Widget status Settled")).toBeInTheDocument();
  });
});
