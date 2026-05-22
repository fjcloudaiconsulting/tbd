import { fireEvent, render, screen } from "@testing-library/react";

import DatePresetChips, {
  buildPresetRanges,
} from "@/components/reports/filters/DatePresetChips";

describe("DatePresetChips", () => {
  it("renders the five preset chips", () => {
    render(<DatePresetChips value={undefined} onChange={() => {}} />);
    expect(screen.getByTestId("date-preset-this_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-ytd")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_12_months")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-custom")).toBeInTheDocument();
  });

  it("fills the correct ISO range when 'This month' is clicked", () => {
    const now = new Date(2026, 4, 15); // May 15, 2026
    const ranges = buildPresetRanges(now);
    const onChange = vi.fn();

    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-this_month"));

    expect(onChange).toHaveBeenCalledWith(ranges.this_month);
    expect(ranges.this_month).toEqual({ start: "2026-05-01", end: "2026-05-31" });
  });

  it("fills the correct ISO range when 'Last month' is clicked", () => {
    const now = new Date(2026, 4, 15); // May 15, 2026
    const onChange = vi.fn();

    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-last_month"));

    expect(onChange).toHaveBeenCalledWith({ start: "2026-04-01", end: "2026-04-30" });
  });

  it("fills the correct ISO range when 'YTD' is clicked", () => {
    const now = new Date(2026, 4, 15); // May 15, 2026
    const onChange = vi.fn();

    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-ytd"));

    expect(onChange).toHaveBeenCalledWith({ start: "2026-01-01", end: "2026-05-15" });
  });

  it("fills the correct ISO range when 'Last 12 months' is clicked", () => {
    const now = new Date(2026, 4, 15); // May 15, 2026
    const onChange = vi.fn();

    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-last_12_months"));

    expect(onChange).toHaveBeenCalledWith({ start: "2025-05-01", end: "2026-05-15" });
  });

  it("opens the date inputs when 'Custom' is clicked", () => {
    const onChange = vi.fn();
    render(<DatePresetChips value={undefined} onChange={onChange} />);
    fireEvent.click(screen.getByTestId("date-preset-custom"));
    // Custom path doesn't auto-fill a range; it just enables the inputs.
    expect(onChange).toHaveBeenCalledWith({});
  });

  it("marks the active preset with aria-pressed", () => {
    const now = new Date(2026, 4, 15);
    const ranges = buildPresetRanges(now);
    render(
      <DatePresetChips value={ranges.this_month} onChange={() => {}} now={now} />,
    );
    expect(
      screen.getByTestId("date-preset-this_month").getAttribute("aria-pressed"),
    ).toBe("true");
  });
});
