import { fireEvent, render, screen } from "@testing-library/react";

import DatePresetChips, {
  buildPresetRanges,
} from "@/components/reports/filters/DatePresetChips";
import { matchPreset } from "@/lib/reports/date-presets";

describe("DatePresetChips", () => {
  it("renders the six preset chips (incl. Next cycle)", () => {
    render(<DatePresetChips value={undefined} onChange={() => {}} />);
    expect(screen.getByTestId("date-preset-this_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_month")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-ytd")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-last_12_months")).toBeInTheDocument();
    expect(screen.getByTestId("date-preset-next_cycle")).toBeInTheDocument();
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

  it("writes only {preset:'next_cycle'} (NOT absolute dates) when Next cycle is clicked", () => {
    const now = new Date(2026, 4, 15);
    const onChange = vi.fn();
    render(<DatePresetChips value={undefined} onChange={onChange} now={now} />);
    fireEvent.click(screen.getByTestId("date-preset-next_cycle"));
    expect(onChange).toHaveBeenCalledWith({ preset: "next_cycle" });
    // The written value carries no absolute window — the backend resolves it.
    const written = onChange.mock.calls[0][0];
    expect(written.start).toBeUndefined();
    expect(written.end).toBeUndefined();
  });

  it("gives the Next cycle chip the 'Next billing cycle' accessible name / title", () => {
    render(<DatePresetChips value={undefined} onChange={() => {}} />);
    const chip = screen.getByTestId("date-preset-next_cycle");
    // Visible text stays terse; the accessible name is precise.
    expect(chip).toHaveTextContent("Next cycle");
    expect(chip).toHaveAttribute("aria-label", "Next billing cycle");
    expect(chip).toHaveAttribute("title", "Next billing cycle");
  });

  it("marks the Next cycle chip active for a {preset:'next_cycle'} value", () => {
    render(
      <DatePresetChips value={{ preset: "next_cycle" }} onChange={() => {}} />,
    );
    expect(
      screen.getByTestId("date-preset-next_cycle").getAttribute("aria-pressed"),
    ).toBe("true");
    // No other chip lights up, and the absolute date inputs stay hidden.
    expect(
      screen.getByTestId("date-preset-this_month").getAttribute("aria-pressed"),
    ).toBe("false");
    expect(screen.queryByTestId("date-preset-from")).not.toBeInTheDocument();
  });

  it("matchPreset returns 'next_cycle' for a token value, before the empty-range guard", () => {
    const now = new Date(2026, 4, 15);
    const ranges = buildPresetRanges(now);
    // Token-only value (no start/end) — must still resolve to next_cycle.
    expect(matchPreset({ preset: "next_cycle" }, ranges)).toBe("next_cycle");
    // A calendar preset still matches its absolute window (regression).
    expect(matchPreset(ranges.this_month, ranges)).toBe("this_month");
  });
});
